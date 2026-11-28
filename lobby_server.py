#!/usr/bin/env python3
"""IFZ 联机大厅（房间目录服务）。

职责只有一个：保存「房间名 / 房间号 -> 房主地址:端口」的映射，让玩家
在主菜单就能搜到朋友开的房。**不中转游戏数据**：搜索完成后客人仍然用
现有的 TCP 直连房主，服务器 347ms 的延迟只影响查房间，不影响战斗手感。

纯 Python 标准库，无第三方依赖。接口：

    GET  /health                       -> {"ok":true,"rooms":N,...}
    GET  /api/servers                  -> 节点列表下发（客户端只内置一个引导地址）
    POST /api/room                     -> 注册，返回 {"code","token","ip","port",...}
    POST /api/room/<code>/heartbeat    -> 保活（需 token）
    DELETE /api/room/<code>?token=...  -> 注销（需 token）
    GET  /api/rooms?q=<关键词>          -> 搜索（按房间号或房间名，包含匹配）

安全边界：
  - 房主 IP 取 TCP 连接的来源地址，客户端不能伪造（除非来源是本机，仅测试用）。
  - token 是 32 位随机 hex，只有持有者能保活/注销，防止别人踢掉房间。
  - TTL 过期自动清理；房间数、单 IP 房间数、房间名长度都有限制。
"""
import io
import json
import os
import secrets
import select
import signal
import socket
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

PORT = int(os.environ.get("IFZ_LOBBY_PORT", "8080"))
_NOFILE = -1
# 网页面板没有命令行时，靠这一行确认「拉到的确实是新版」：浏览器打开 /health 就能看到。
VERSION = "2026-11-28.5"
DATA = os.environ.get("IFZ_LOBBY_DATA", "/root/ifz-lobby/rooms.json")

# ---- 运行时遥测 ----
# 客户端把「发生了什么」脱敏后批量发过来，这里按 天/会话 归档成 jsonl。
# 归档目录默认放在 rooms.json 旁边，容器重建只要挂载目录在就不会丢。
TELEMETRY_DIR = os.environ.get(
    "IFZ_TELEMETRY_DIR", os.path.join(os.path.dirname(DATA) or ".", "telemetry"))
TELEMETRY_MAX_BODY = int(os.environ.get("IFZ_TELEMETRY_MAX_BODY", str(160 * 1024)))
TELEMETRY_PER_MIN = int(os.environ.get("IFZ_TELEMETRY_PER_MIN", "20"))
TELEMETRY_KEEP_DAYS = int(os.environ.get("IFZ_TELEMETRY_KEEP_DAYS", "14"))
TELEMETRY_MAX_SESSIONS_DAY = int(os.environ.get("IFZ_TELEMETRY_MAX_SESSIONS", "2000"))

# ---- 运维面板 ----
# 设了 IFZ_ADMIN_TOKEN 才开放 /admin。公网机器不设就是 403，这是刻意的：
# 一个能看日志、能下载数据的面板不该靠「别人猜不到路径」来保护。
ADMIN_TOKEN_ENV = os.environ.get("IFZ_ADMIN_TOKEN", "").strip()
ADMIN_TOKEN_FILE = "admin.token"


def _admin_token():
    """管理令牌。环境变量优先，其次读 DATA 同目录的 admin.token。

    读文件是为了「不用改面板环境变量、不用重启」——把文件 SFTP 上去就生效。
    每次请求都读：面板的请求量是几十次/分钟，文件只有几字节。
    """
    if ADMIN_TOKEN_ENV:
        return ADMIN_TOKEN_ENV
    path = os.path.join(os.path.dirname(DATA) or ".", ADMIN_TOKEN_FILE)
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    except OSError:
        pass
    return ""
ADMIN_LOG_LINES_MAX = 2000


# ---- 节点列表下发 ----
# 玩家客户端只内置一个「引导地址」，真正有哪些大厅由这里说了算。加一台机器＝改
# servers.json，不用重新编译客户端、不用发版，也不用管玩家本地那份 modpack.json。
# 文件缺失或写坏了就回退到 DEFAULT_SERVERS：下发故障不该让人开不了房。
SERVERS_FILE = os.environ.get(
    "IFZ_SERVERS_FILE", os.path.join(os.path.dirname(DATA) or ".", "servers.json"))
DEFAULT_SERVERS = [
    "http://host.noblehost.cc:30811",
    "http://de3.nodaroo.fun:28068",
]
# 条数和长度都限死：玩家端会照单全收，一份被塞了垃圾的列表不该拖慢启动。
MAX_SERVERS = 8
MAX_SERVER_URL = 200
# 客户端缓存多久（秒）。给的是保守值，改列表不用等它过期——客户端会后台刷新。
SERVER_LIST_TTL = 3600

_servers_lock = threading.Lock()
_servers_cache = {"stamp": None, "list": list(DEFAULT_SERVERS)}


def _clean_server_url(u):
    """只接受 http/https 的绝对地址。空白、引号、尖括号一律拒绝。"""
    if not isinstance(u, str):
        return None
    u = u.strip().rstrip("/")
    if not u or len(u) > MAX_SERVER_URL:
        return None
    for ch in " \t\r\n\"'<>\\":
        if ch in u:
            return None
    low = u.lower()
    for scheme in ("http://", "https://"):
        if low.startswith(scheme):
            return u if len(u) > len(scheme) else None
    return None


def _servers_list():
    """读 servers.json（按 mtime 缓存），读不到或全被拒就用代码里的默认值。"""
    try:
        st = os.stat(SERVERS_FILE)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
        # 第一次跑（或换了机器）就把默认列表落成文件：这样运维有一个现成的位置可以
        # SFTP 覆盖，不用猜格式。只在文件不存在时写，绝不覆盖别人改过的。
        try:
            with open(SERVERS_FILE, "w", encoding="utf-8") as f:
                json.dump({"servers": DEFAULT_SERVERS}, f, ensure_ascii=False, indent=2)
            st = os.stat(SERVERS_FILE)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
    with _servers_lock:
        if stamp is not None and _servers_cache["stamp"] == stamp:
            return list(_servers_cache["list"])
    out = []
    if stamp is not None:
        try:
            with open(SERVERS_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("servers") if isinstance(raw, dict) else raw
            if isinstance(items, list):
                for it in items:
                    if len(out) >= MAX_SERVERS:
                        break
                    one = _clean_server_url(it)
                    if one and one not in out:
                        out.append(one)
        except (OSError, ValueError, AttributeError):
            out = []
    if not out:
        out = list(DEFAULT_SERVERS)
    with _servers_lock:
        _servers_cache["stamp"] = stamp
        _servers_cache["list"] = out
    return list(out)


ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IFZ 运维面板</title>
<style>
:root{
  --bg:#0a0e13; --card:#121820; --card2:#182130; --line:#233040;
  --txt:#d7e3ef; --dim:#7f93a8; --accent:#4ea1d3; --ok:#4ec98a;
  --warn:#e0b64a; --bad:#e26565; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 20% -10%,#16243a 0%,var(--bg) 55%);
  color:var(--txt);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif;
  min-height:100vh}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 60px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:19px;margin:0;letter-spacing:.5px;font-weight:600}
h1 b{color:var(--accent)}
.meta{color:var(--dim);font-size:12.5px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);display:inline-block;
  box-shadow:0 0 8px var(--ok)}
.dot.bad{background:var(--bad);box-shadow:0 0 8px var(--bad)}
.spacer{flex:1}
button,select{background:var(--card2);color:var(--txt);border:1px solid var(--line);
  border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer;transition:.15s}
button:hover{border-color:var(--accent);color:#fff}
button.on{background:var(--accent);border-color:var(--accent);color:#04121c;font-weight:600}
button.ghost{background:transparent}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.stat{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);
  border-radius:12px;padding:14px 16px;position:relative;overflow:hidden}
.stat:after{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--accent);opacity:.7}
.stat.ok:after{background:var(--ok)} .stat.warn:after{background:var(--warn)}
.stat.bad:after{background:var(--bad)}
.stat .k{color:var(--dim);font-size:12px;letter-spacing:.4px}
.stat .v{font-size:25px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.stat .s{color:var(--dim);font-size:11.5px;margin-top:2px}
.tabs{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
  margin-bottom:14px}
.card h3{margin:0 0 12px;font-size:13px;color:var(--dim);font-weight:600;letter-spacing:.6px;
  text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;font-size:11.5px;letter-spacing:.5px;
  padding:6px 8px;border-bottom:1px solid var(--line);text-transform:uppercase}
td{padding:7px 8px;border-bottom:1px solid #1a2431;vertical-align:middle}
tr:hover td{background:#151d28}
.mono{font-family:var(--mono);font-size:12.5px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;
  border:1px solid var(--line);color:var(--dim)}
.pill.ok{color:var(--ok);border-color:#1f4a37;background:#102a1f}
.pill.bad{color:var(--bad);border-color:#4a2020;background:#2a1414}
.pill.warn{color:var(--warn);border-color:#4a3d1f;background:#2a2414}
.bars{display:flex;flex-direction:column;gap:7px}
.bar{display:grid;grid-template-columns:minmax(120px,240px) 1fr 54px;gap:10px;align-items:center;
  font-size:12.5px}
.bar .n{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .t{background:#0d141c;border-radius:5px;height:16px;overflow:hidden}
.bar .f{height:100%;background:linear-gradient(90deg,#2b6f95,#4ea1d3);border-radius:5px}
.bar .f.bad{background:linear-gradient(90deg,#7a2b2b,#e26565)}
.bar .v{text-align:right;font-variant-numeric:tabular-nums;color:var(--txt)}
pre{background:#080c11;border:1px solid var(--line);border-radius:10px;padding:14px;
  overflow:auto;max-height:58vh;font-family:var(--mono);font-size:12px;line-height:1.6;
  margin:0;white-space:pre-wrap;word-break:break-word}
.empty{color:var(--dim);font-size:13px;padding:18px 4px}
.hidden{display:none!important}
.drawer{position:fixed;inset:0;background:#0008;backdrop-filter:blur(2px);z-index:50;
  display:flex;justify-content:flex-end}
.drawer .body{width:min(880px,94vw);height:100%;background:var(--bg);border-left:1px solid var(--line);
  padding:18px 20px;overflow:auto;display:flex;flex-direction:column;gap:12px}
.drawer header{margin:0}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input[type=text],input[type=password]{background:#0d141c;border:1px solid var(--line);color:var(--txt);
  border-radius:8px;padding:8px 12px;font-size:13px;min-width:220px}
input:focus{outline:none;border-color:var(--accent)}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:var(--card2);
  border:1px solid var(--accent);color:var(--txt);padding:9px 16px;border-radius:9px;font-size:13px;
  opacity:0;transition:.25s;pointer-events:none;z-index:80}
.toast.show{opacity:1}
.gate{max-width:420px;margin:14vh auto;text-align:center}
.gate h2{font-size:17px;margin:0 0 8px}
.gate p{color:var(--dim);font-size:13px;margin:0 0 16px;line-height:1.7}
</style>
</head>
<body>
<div class="wrap">
  <div id="gate" class="gate hidden">
    <h2>需要管理令牌</h2>
    <p>服务器设了 <code>IFZ_ADMIN_TOKEN</code>。<br>输入令牌后可访问；它会存在本机浏览器里。</p>
    <div class="row" style="justify-content:center">
      <input id="tok" type="password" placeholder="管理令牌" autocomplete="off">
      <button onclick="saveToken()">进入</button>
    </div>
  </div>

  <div id="main" class="hidden">
    <header>
      <span class="dot" id="dot"></span>
      <h1><b>IFZ</b> 运维面板</h1>
      <div class="meta" id="meta"></div>
      <span class="spacer"></span>
      <button id="auto" class="on" onclick="toggleAuto()">自动 5s</button>
      <button onclick="loadAll(true)">刷新</button>
    </header>

    <div class="stats" id="stats"></div>

    <div class="tabs">
      <button class="on" data-tab="overview" onclick="switchTab('overview')">概览</button>
      <button data-tab="rooms" onclick="switchTab('rooms')">房间</button>
      <button data-tab="telemetry" onclick="switchTab('telemetry')">遥测</button>
      <button data-tab="logs" onclick="switchTab('logs')">日志</button>
    </div>

    <div id="tab-overview">
      <div class="card"><h3>联机链路</h3><div class="bars" id="linkbars"></div></div>
      <div class="card"><h3>没生效的层（钩子数 = 0）</h3><div class="bars" id="zerobars"></div></div>
      <div class="card"><h3>断线原因</h3><div class="bars" id="dropbars"></div></div>
    </div>

    <div id="tab-rooms" class="hidden">
      <div class="card">
        <div class="row" style="margin-bottom:10px">
          <input id="q" type="text" placeholder="按房间名 / 房间号过滤" oninput="renderRooms()">
          <span class="spacer"></span>
          <span class="pill" id="roomcount"></span>
        </div>
        <table><thead><tr>
          <th>房间号</th><th>名称</th><th>城市</th><th>地址</th><th>端口</th>
          <th>人数</th><th>中转</th><th>更新</th>
        </tr></thead><tbody id="roomrows"></tbody></table>
      </div>
    </div>

    <div id="tab-telemetry" class="hidden">
      <div class="card">
        <div class="row">
          <select id="day"></select>
          <button onclick="loadSessions()">读取</button>
          <span class="spacer"></span>
          <button class="ghost" onclick="downloadZip()">下载这一天的全部日志</button>
        </div>
      </div>
      <div class="card"><h3>版本分布</h3><div class="bars" id="modbars"></div></div>
      <div class="card"><h3>系统分布</h3><div class="bars" id="osbars"></div></div>
      <div class="card"><h3>错误 Top</h3><div class="bars" id="errbars"></div></div>
      <div class="card">
        <h3>会话</h3>
        <table><thead><tr>
          <th>时间</th><th>会话</th><th>版本</th><th>系统</th>
          <th>事件</th><th>错误</th><th>时长</th><th></th>
        </tr></thead><tbody id="sessrows"></tbody></table>
      </div>
    </div>

    <div id="tab-logs" class="hidden">
      <div class="card">
        <div class="row" style="margin-bottom:12px">
          <select id="logname">
            <option value="supervisor.log">supervisor.log</option>
            <option value="lobby.log">lobby.log</option>
            <option value="supervisor.log.1">supervisor.log.1</option>
          </select>
          <select id="loglines">
            <option>100</option><option selected>200</option>
            <option>500</option><option>1000</option><option>2000</option>
          </select>
          <button onclick="loadLog()">读取</button>
          <span class="spacer"></span>
          <button class="ghost" onclick="downloadLog()">下载</button>
        </div>
        <pre id="logbox">—</pre>
      </div>
    </div>
  </div>
</div>

<div id="drawer" class="drawer hidden">
  <div class="body">
    <header>
      <h1 style="font-size:16px" id="dtitle">会话详情</h1>
      <span class="spacer"></span>
      <button onclick="closeDrawer()">关闭</button>
    </header>
    <div class="meta" id="dmeta"></div>
    <div class="row">
      <select id="dlvl" onchange="renderEvents()">
        <option value="">全部</option><option value="error">只看错误</option>
        <option value="warn">只看警告</option><option value="info">只看信息</option>
      </select>
      <span class="pill" id="devcount"></span>
      <span class="spacer"></span>
      <button class="ghost" onclick="downloadSession()">下载这个会话</button>
    </div>
    <pre id="devents"></pre>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
var TOKEN = localStorage.getItem("ifz_admin") || "";
var tab = "overview", auto = true, timer = null;
var data = { overview:null, rooms:[], sessions:[], detail:null, day:"" };
var lastRooms = [];

function $(id){ return document.getElementById(id); }
function esc(s){
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function toast(msg){
  var t = $("toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(function(){ t.classList.remove("show"); }, 1800);
}
function saveToken(){
  TOKEN = $("tok").value.trim();
  localStorage.setItem("ifz_admin", TOKEN);
  boot();
}
function api(path){
  return fetch(path, { headers: { "X-Admin-Token": TOKEN } }).then(function(r){
    if (r.status === 401 || r.status === 403) { throw { auth: true }; }
    if (!r.ok) { throw new Error("HTTP " + r.status); }
    return r.json();
  });
}
function withTok(url){
  return url + (url.indexOf("?") >= 0 ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
}

function boot(){
  $("gate").classList.add("hidden");
  $("main").classList.add("hidden");
  api("/admin/api/overview").then(function(d){
    $("main").classList.remove("hidden");
    data.overview = d;
    renderOverview();
    loadRooms();
    loadDays();
    if (!timer && auto) timer = setInterval(tick, 5000);
  }).catch(function(e){
    if (e && e.auth) { $("gate").classList.remove("hidden"); }
    else { $("gate").classList.remove("hidden"); $("tok").placeholder = "无法连接服务端"; }
  });
}
function tick(){ if (auto) loadAll(false); }
function toggleAuto(){
  auto = !auto;
  $("auto").classList.toggle("on", auto);
  $("auto").textContent = auto ? "自动 5s" : "已暂停";
  if (auto && !timer) timer = setInterval(tick, 5000);
  if (!auto && timer) { clearInterval(timer); timer = null; }
}
function switchTab(name){
  tab = name;
  ["overview","rooms","telemetry","logs"].forEach(function(k){
    $("tab-" + k).classList.toggle("hidden", k !== name);
    document.querySelector('[data-tab="' + k + '"]').classList.toggle("on", k === name);
  });
  if (name === "telemetry" && !data.sessions.length) loadSessions();
  if (name === "logs") loadLog();
}

function fmtDur(s){
  if (!s) return "—";
  if (s < 90) return Math.round(s) + " 秒";
  if (s < 5400) return Math.round(s / 60) + " 分";
  return (s / 3600).toFixed(1) + " 小时";
}
function bar(el, rows, cls){
  var max = 1;
  rows.forEach(function(r){ if (r[1] > max) max = r[1]; });
  if (!rows.length) { el.innerHTML = '<div class="empty">暂无数据</div>'; return; }
  el.innerHTML = rows.slice(0, 8).map(function(r){
    var pct = Math.max(2, Math.round(r[1] * 100 / max));
    return '<div class="bar"><span class="n" title="' + esc(r[0]) + '">' + esc(r[0]) + '</span>' +
      '<span class="t"><span class="f ' + (cls || "") + '" style="width:' + pct + '%"></span></span>' +
      '<span class="v">' + r[1] + '</span></div>';
  }).join("");
}
function pairs(o){
  var out = [];
  for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) out.push([k, o[k]]);
  out.sort(function(a, b){ return b[1] - a[1]; });
  return out;
}

function renderOverview(){
  var d = data.overview; if (!d) return;
  var m = d.metrics || {}, s = d.telstats || {}, sum = d.summary || {};
  var up = m.uptime_sec || 0;
  $("dot").className = "dot" + (m.ok ? "" : " bad");
  $("meta").innerHTML =
    "<span>" + esc(d.host) + "</span><span>·</span><span>版本 " + esc(m.version || "?") + "</span>" +
    "<span>·</span><span>运行 " + fmtDur(up) + "</span>";
  var cards = [
    ["房间", m.rooms, "/ " + (m.limits ? m.limits.max_rooms : "?") + " 上限", m.rooms > 0 ? "ok" : ""],
    ["中转对", m.pairs, (m.free_seats || 0) + " 个空位", m.pairs > 0 ? "ok" : ""],
    ["带宽", (m.mbps_since_last || 0).toFixed(2), "Mbps（近一分钟）", ""],
    ["线程", m.threads, "nofile " + (m.nofile > 0 ? m.nofile : "?"), ""],
    ["今日会话", sum.sessions || 0, "事件 " + (sum.events || 0), ""],
    ["错误", sum.errors || 0, "警告 " + (sum.warns || 0), (sum.errors || 0) > 0 ? "bad" : "ok"],
    ["拒绝注册", ((m.rejects && (m.rejects.full + m.rejects.ip + m.rejects.bad)) || 0), "满/限流/非法", ""],
    ["遥测积压", (s.pending || 0), "已归档 " + ((s.bytes || 0) / 1024).toFixed(0) + " KB", ""]
  ];
  $("stats").innerHTML = cards.map(function(c){
    return '<div class="stat ' + c[3] + '"><div class="k">' + c[0] + '</div>' +
      '<div class="v">' + esc(c[1]) + '</div><div class="s">' + esc(c[2]) + '</div></div>';
  }).join("");

  var net = pairs(m.link_kinds || {});
  bar($("linkbars"), net.length ? net : [["直连", 0], ["中转", 0], ["Steam", 0]], "");
  bar($("zerobars"), pairs(sum.layer_zero || {}), "bad");
  bar($("dropbars"), pairs(sum.drop || {}), "bad");
  renderDist();
}
function renderDist(){
  var d = data.overview; if (!d) return;
  var sum = d.summary || {};
  bar($("modbars"), pairs(sum.mod || {}), "");
  bar($("osbars"), pairs(sum.os || {}), "");
  bar($("errbars"), pairs(sum.error || {}), "bad");
}

function loadAll(manual){
  api("/admin/api/overview").then(function(d){
    data.overview = d; renderOverview();
    if (manual) toast("已刷新");
  }).catch(function(){ $("dot").className = "dot bad"; });
  loadRooms();
  if (tab === "telemetry" && data.day) loadSessions();
  if (tab === "logs") loadLog();
}
function loadRooms(){
  api("/admin/api/rooms").then(function(d){
    lastRooms = d.rooms || [];
    renderRooms();
  }).catch(function(){});
}
function renderRooms(){
  var q = ($("q").value || "").toLowerCase();
  var rows = lastRooms.filter(function(r){
    return !q || (r.name || "").toLowerCase().indexOf(q) >= 0
      || (r.code || "").toLowerCase().indexOf(q) >= 0;
  });
  $("roomcount").textContent = rows.length + " 间";
  if (!rows.length) {
    $("roomrows").innerHTML = '<tr><td colspan="8" class="empty">没有房间</td></tr>';
    return;
  }
  $("roomrows").innerHTML = rows.map(function(r){
    return "<tr><td class='mono'>" + esc(r.code) + "</td><td>" + esc(r.name) + "</td>" +
      "<td>" + esc(r.city || "—") + "</td><td class='mono'>" + esc(r.ip || "—") + "</td>" +
      "<td class='mono'>" + esc(r.port) + "</td><td>" + esc(r.players) + "</td>" +
      "<td>" + (r.relay ? "<span class='pill ok'>" + esc(r.relay) + "</span>" : "<span class='pill'>—</span>") + "</td>" +
      "<td class='mono'>" + esc(r.age) + "s</td></tr>";
  }).join("");
}

function loadDays(){
  var d = data.overview; if (!d) return;
  var days = d.days || [];
  if (!days.length) return;
  $("day").innerHTML = days.map(function(x){
    return '<option value="' + esc(x) + '">' + esc(x) + '</option>';
  }).join("");
  data.day = days[0];
  $("day").onchange = function(){ data.day = this.value; loadSessions(); };
}
function loadSessions(){
  if (!data.day) return;
  api("/admin/api/sessions?day=" + encodeURIComponent(data.day)).then(function(d){
    data.sessions = d.sessions || [];
    renderSessions();
    renderDist();
  }).catch(function(){});
}
function renderSessions(){
  var rows = data.sessions;
  if (!rows.length) {
    $("sessrows").innerHTML = '<tr><td colspan="8" class="empty">这一天还没有数据</td></tr>';
    return;
  }
  $("sessrows").innerHTML = rows.map(function(r){
    var t = new Date(r.mtime * 1000);
    var hh = ("0" + t.getHours()).slice(-2) + ":" + ("0" + t.getMinutes()).slice(-2);
    return "<tr><td class='mono'>" + hh + "</td>" +
      "<td class='mono'>" + esc(r.session.slice(0, 10)) + "</td>" +
      "<td>" + esc(String(r.mod).replace("IFZ.ModPack ", "")) + "</td>" +
      "<td class='mono'>" + esc(String(r.os).slice(0, 22)) + "</td>" +
      "<td>" + r.events + "</td>" +
      "<td>" + (r.errors ? "<span class='pill bad'>" + r.errors + "</span>" : "0") + "</td>" +
      "<td>" + fmtDur(r.seconds) + "</td>" +
      "<td><button onclick=\"openSession('" + esc(r.session) + "')\">详情</button></td></tr>";
  }).join("");
}
function openSession(id){
  api("/admin/api/session?day=" + encodeURIComponent(data.day) + "&id=" + encodeURIComponent(id))
    .then(function(d){
      data.detail = d;
      $("dtitle").textContent = "会话 " + id.slice(0, 12);
      var env = d.env || {};
      $("dmeta").innerHTML = [
        esc(env.mod || "?"), esc(env.os || "?"), esc(env.game || "?"),
        (env.cores || "?") + " 核", (env.mem_mb || "?") + " MB",
        esc(env.screen || "?"), "IP " + esc(d.ip || "?"),
        d.batches + " 批 / " + d.total + " 条"
      ].join(" · ");
      $("drawer").classList.remove("hidden");
      renderEvents();
    }).catch(function(){ toast("读取失败"); });
}
function renderEvents(){
  var d = data.detail; if (!d) return;
  var lvl = $("dlvl").value;
  var evs = (d.events || []).filter(function(e){ return !lvl || e.lvl === lvl; });
  $("devcount").textContent = evs.length + " 条";
  $("devents").textContent = evs.map(function(e){
    var f = e.f ? " " + JSON.stringify(e.f) : "";
    var err = e.err ? "\n    " + e.err : "";
    var tag = e.lvl === "error" ? "ERR " : e.lvl === "warn" ? "WRN " : "    ";
    return tag + ("     " + e.t).slice(-8) + "  " + e.cat + "." + e.ev + f + err;
  }).join("\n") || "（没有匹配的事件）";
}
function closeDrawer(){ $("drawer").classList.add("hidden"); }
function downloadSession(){
  var d = data.detail; if (!d) return;
  location.href = withTok("/api/telemetry/download?day=" + encodeURIComponent(data.day) +
    "&session=" + encodeURIComponent(d.session));
}
function downloadZip(){
  if (!data.day) return;
  location.href = withTok("/api/telemetry/download?day=" + encodeURIComponent(data.day));
}
function loadLog(){
  var name = $("logname").value, lines = $("loglines").value;
  api("/admin/api/logs?name=" + encodeURIComponent(name) + "&lines=" + lines)
    .then(function(d){
      $("logbox").textContent = d.text || "（空）";
      $("logbox").scrollTop = $("logbox").scrollHeight;
    }).catch(function(e){
      $("logbox").textContent = "读取失败：" + (e.message || e);
    });
}
function downloadLog(){
  var name = $("logname").value;
  location.href = withTok("/admin/api/logs?name=" + encodeURIComponent(name) +
    "&lines=" + $("loglines").value + "&download=1");
}
document.addEventListener("keydown", function(e){
  if (e.key === "Escape") closeDrawer();
});
document.addEventListener("DOMContentLoaded", function(){
  $("tok").addEventListener("keydown", function(e){ if (e.key === "Enter") saveToken(); });
  if (TOKEN) boot(); else $("gate").classList.remove("hidden");
});
</script>
</body>
</html>
"""

TTL = int(os.environ.get("IFZ_LOBBY_TTL", "90"))        # 秒无心跳即过期
# 房间数上限按带宽反推：8 人满房约 5 Mbps，120 房远高于单机出口能力，
# 真到瓶颈时先看 /metrics 的 mbps，而不是等房间号用完。
MAX_ROOMS = int(os.environ.get("IFZ_LOBBY_MAX", "120"))
# 网吧/校园网/公司共用出口 IP：3 间太紧，公开后第一批投诉就是这个。默认 8。
MAX_PER_IP = int(os.environ.get("IFZ_LOBBY_MAX_PER_IP", "8"))
MAX_NAME = 24
# 去掉 O/0/I/1 等易混字符
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 6

# 每台服务器为同一个房间预留几个客人座位。
# 座位是「一条隧道服务一个客人」的 slot 模型，房主开房时一次性建满，所以这个数
# 决定单台能接几个客人。两台一起跑时由房主在两台上各建一组，客人按空闲座位分流。
# 7 座位 = 房主 + 7 个客人 = MaxPeers 的满员；单台就能带满一个 8 人房。
# 7 × 56KB/s ≈ 3.1Mbps 下行，仍在单台出口能力内。两台一起跑时各开 7 条，
# 多出来的那组是冗余：任意一台挂掉，另一台仍然接得下整房。
MAX_RELAY_GUESTS = int(os.environ.get("IFZ_RELAY_SEATS", "7"))

_lock = threading.RLock()
_rooms = {}      # code -> {name, token, ip, port, city, players, updated, steam, relay}
_relay_rooms = {}          # relay 码 -> {"hosts":[MAX_RELAY_GUESTS 个房主隧道], "pairs":[(host,guest)], "created":ts}
_relay_lock = threading.Lock()

# ---- 运行指标：只做累加，读的时候加锁，不上报、不写盘 ----
_stats_lock = threading.Lock()
_stats = {
    "since": time.time(),
    "bytes": 0,
    "tunnels": 0,
    "pairs": 0,
    "reject_full": 0,
    "reject_ip": 0,
    "reject_bad": 0,
    "registrations": 0,
}
_metrics_last = {"ts": time.time(), "bytes": 0}


def _count(key, n=1):
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


def _note_bytes(n):
    with _stats_lock:
        _stats["bytes"] += n


def _raise_nofile():
    """每个中转客人占 2 个 socket，默认 1024 撑不到 500 人。启动时顶到 65535。"""
    try:
        import resource
        want = int(os.environ.get("IFZ_NOFILE", "65535"))
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = want if hard == resource.RLIM_INFINITY else min(want, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        return -1


def _gauge():
    """现算的瞬时量：房间、中转房间、已配对客人、可用座位。"""
    with _lock:
        rooms = len(_rooms)
    with _relay_lock:
        relay_rooms = len(_relay_rooms)
        pairs = sum(len(st["pairs"]) for st in _relay_rooms.values())
        free = sum(1 for st in _relay_rooms.values()
                   for h in st["hosts"] if h is None or not h.paired)
    return rooms, relay_rooms, pairs, free


def _mbps_since_last():
    """上一次抓取至今的平均速率。抓取方按固定间隔拉，就能得到真实曲线。"""
    with _stats_lock:
        now = time.time()
        dt = now - _metrics_last["ts"]
        db = _stats["bytes"] - _metrics_last["bytes"]
        _metrics_last["ts"] = now
        _metrics_last["bytes"] = _stats["bytes"]
    if dt <= 0:
        return 0.0
    return round(db * 8.0 / (dt * 1000000.0), 3)


# ---------------------------------------------------------------- telemetry
_tel_lock = threading.Lock()
_tel_rate = {}
_tel_stats = {"sessions": set(), "events": 0, "bytes": 0,
              "rejects": {"rate": 0, "size": 0, "bad": 0}}
_tel_cache = {"at": 0.0, "days": 0, "body": None}


def _tel_day(offset=0):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - offset * 86400))


def _tel_safe_session(value):
    """会话 id 会直接进文件名，必须是纯 hex，长度受限——否则就是路径穿越。"""
    if not isinstance(value, str) or not 8 <= len(value) <= 64:
        return None
    return value if all(c in "0123456789abcdef" for c in value) else None


def _tel_allow(ip):
    """单 IP 每分钟 N 次。不做的话一个死循环客户端能把磁盘写满。"""
    now = time.time()
    with _tel_lock:
        if len(_tel_rate) > 8000:
            _tel_rate.clear()
        hits = [t for t in _tel_rate.get(ip, []) if now - t < 60.0]
        if len(hits) >= TELEMETRY_PER_MIN:
            _tel_rate[ip] = hits
            _tel_stats["rejects"]["rate"] += 1
            return False
        hits.append(now)
        _tel_rate[ip] = hits
        return True


def _tel_store(session, body):
    """按天/会话追加一行。原子追加，多台客户端并发也不会写坏。"""
    day = _tel_day()
    folder = os.path.join(TELEMETRY_DIR, day)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, session + ".jsonl")
    line = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    with _tel_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _tel_stats["sessions"].add(day + "/" + session)
        _tel_stats["events"] += len(body.get("events") or [])
        _tel_stats["bytes"] += len(line)
        _tel_cache["at"] = 0.0      # 有新数据进来，汇总立刻失效
    return len(line)


def _tel_prune():
    """按天清理。只在写盘时顺带做一次，不单独开线程。"""
    try:
        if not os.path.isdir(TELEMETRY_DIR):
            return
        cutoff = _tel_day(TELEMETRY_KEEP_DAYS)
        for name in os.listdir(TELEMETRY_DIR):
            full = os.path.join(TELEMETRY_DIR, name)
            if os.path.isdir(full) and name < cutoff:
                for f in os.listdir(full):
                    try:
                        os.remove(os.path.join(full, f))
                    except OSError:
                        pass
                try:
                    os.rmdir(full)
                except OSError:
                    pass
    except Exception:
        pass


def _admin_ok(headers, query):
    """面板鉴权。没配 token 一律拒绝，绝不放行「空 token 等于无鉴权」。"""
    token = _admin_token()
    if not token:
        return False
    given = (query.get("token", [""])[0] or headers.get("X-Admin-Token", "") or "").strip()
    if not given:
        return False
    return secrets.compare_digest(given, token)


def _admin_sessions(day):
    """某个日期下的会话清单。带 60 秒缓存，别让面板刷新把磁盘读爆。"""
    folder = os.path.join(TELEMETRY_DIR, day)
    if not os.path.isdir(folder):
        return []
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    for name in names[:TELEMETRY_MAX_SESSIONS_DAY]:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(folder, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        env, events, errors, first, last, ip = None, 0, 0, None, None, ""
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    if env is None:
                        env = payload.get("env")
                        ip = payload.get("ip", "")
                    evs = payload.get("events") or []
                    events += len(evs)
                    for ev in evs:
                        if ev.get("lvl") == "error":
                            errors += 1
                        t = ev.get("t")
                        if isinstance(t, (int, float)):
                            if first is None or t < first:
                                first = t
                            if last is None or t > last:
                                last = t
        except OSError:
            continue
        env = env or {}
        out.append({
            "session": name[:-6],
            "bytes": st.st_size,
            "mtime": int(st.st_mtime),
            "events": events,
            "errors": errors,
            "ip": ip,
            "mod": env.get("mod", "?"),
            "os": env.get("os", "?"),
            "game": env.get("game", "?"),
            "seconds": round((last - first), 1) if (first is not None and last is not None) else 0,
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _tel_zip(day, session=""):
    """把归档打成 zip 返回字节。单个会话给 jsonl，整天给全部会话。"""
    folder = os.path.join(TELEMETRY_DIR, day) if day else TELEMETRY_DIR
    if not os.path.isdir(folder):
        return None, "not found"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if session:
            safe = _tel_safe_session(session)
            if safe is None:
                return None, "bad session"
            path = os.path.join(folder, safe + ".jsonl")
            if not os.path.isfile(path):
                return None, "not found"
            z.write(path, arcname=safe + ".jsonl")
        else:
            for name in sorted(os.listdir(folder)):
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    z.write(path, arcname="%s/%s" % (day or "telemetry", name))
    return buf.getvalue(), ""


def _tel_mask_ip(ip):
    """服务器再擦一次 IP：客户端可能漏，而且来源地址也不该原样落盘。"""
    parts = (ip or "").split(".")
    if len(parts) == 4 and all(x.isdigit() for x in parts):
        return parts[0] + "." + parts[1] + ".x.x"
    return "v6" if ":" in (ip or "") else "?"


def _tel_session(day, session, limit=600):
    """取单个会话的事件流（末尾 limit 条）。用来复现某个玩家的完整一局。"""
    path = os.path.join(TELEMETRY_DIR, day, session + ".jsonl")
    if not os.path.isfile(path):
        return {"ok": False, "error": "not found", "day": day, "session": session}
    env, events, batches, ip = None, [], 0, ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                batches += 1
                if env is None:
                    env = payload.get("env")
                    ip = payload.get("ip", "")
                events.extend(payload.get("events") or [])
    except OSError as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": True, "day": day, "session": session, "ip": ip,
            "env": env, "batches": batches, "total": len(events),
            "events": events[-limit:]}


def _tel_bump(bucket, key, n=1):
    bucket[key] = bucket.get(key, 0) + n


def _tel_summary(days=7):
    """把最近 N 天的归档折成一份可读分布。带 60 秒缓存，别让面板刷新打爆磁盘。"""
    with _tel_lock:
        if (_tel_cache["body"] is not None and _tel_cache["days"] == days
                and time.time() - _tel_cache["at"] < 60.0):
            return _tel_cache["body"]

    out = {"ok": True, "version": VERSION, "days": days, "sessions": 0,
           "events": 0, "errors": 0, "warns": 0,
           "mod": {}, "os": {}, "game": {}, "lang": {}, "cores": {},
           "event": {}, "error": {}, "drop": {},
           "reconnect_ok": 0, "reconnect_giveup": 0,
           "layer_zero": {}, "lobby_none": 0, "host_listen_failed": 0}
    for i in range(days):
        folder = os.path.join(TELEMETRY_DIR, _tel_day(i))
        if not os.path.isdir(folder):
            continue
        try:
            names = sorted(os.listdir(folder))[:TELEMETRY_MAX_SESSIONS_DAY]
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            out["sessions"] += 1
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except ValueError:
                            continue
                        _tel_absorb(out, payload)
            except OSError:
                continue
    with _tel_lock:
        _tel_cache["body"] = out
        _tel_cache["at"] = time.time()
        _tel_cache["days"] = days
    return out


def _tel_absorb(out, payload):
    env = payload.get("env") or {}
    _tel_bump(out["mod"], str(env.get("mod", "?")))
    _tel_bump(out["os"], str(env.get("os", "?")))
    _tel_bump(out["game"], str(env.get("game", "?")))
    _tel_bump(out["lang"], str(env.get("lang", "?")))
    _tel_bump(out["cores"], str(env.get("cores", "?")))
    net = env.get("net") or {}
    for kind in ("direct", "relay", "steam", "fail", "drops"):
        try:
            n = int(net.get(kind) or 0)
        except (TypeError, ValueError):
            continue
        if n:
            key = "link_" + kind
            out[key] = out.get(key, 0) + n

    for ev in payload.get("events") or []:
        cat = str(ev.get("cat", "?"))
        name = str(ev.get("ev", "?"))
        key = cat + "." + name
        out["events"] += 1
        _tel_bump(out["event"], key)
        lvl = ev.get("lvl")
        if lvl == "error":
            out["errors"] += 1
            _tel_bump(out["error"], key)
        elif lvl == "warn":
            out["warns"] += 1
        if cat == "net" and name == "drop":
            fields = ev.get("f") or {}
            _tel_bump(out["drop"], str(fields.get("phase", "?")) + ":" +
                      str(fields.get("reason", "?"))[:40])
        elif cat == "net" and name == "reconnect.ok":
            out["reconnect_ok"] += 1
        elif cat == "net" and name == "reconnect.gave-up":
            out["reconnect_giveup"] += 1
        elif cat == "net" and name == "host.listen-failed":
            out["host_listen_failed"] += 1
        elif cat == "lobby" and name == "register.none":
            out["lobby_none"] += 1
        elif cat == "layer" and name == "applied":
            fields = ev.get("f") or {}
            for k, v in fields.items():
                if isinstance(v, int) and v == 0 and k not in ("menu",):
                    _tel_bump(out["layer_zero"], k)


def _metrics_snapshot():
    """一次算齐所有指标。面板与 /metrics 共用，避免两处算法漂移。"""
    with _lock:
        _prune_locked()
    rooms, relay_rooms, pairs, free = _gauge()
    mbps = _mbps_since_last()
    with _stats_lock:
        snap = dict(_stats)
        uptime = int(time.time() - snap["since"])
    return {
        "ok": True, "version": VERSION, "uptime_sec": uptime,
        "rooms": rooms, "relay_rooms": relay_rooms,
        "pairs": pairs, "free_seats": free,
        "mbps_since_last": mbps,
        "bytes_relayed": snap["bytes"],
        "tunnels_opened": snap["tunnels"],
        "pairs_made": snap["pairs"],
        "registrations": snap["registrations"],
        "rejects": {"full": snap["reject_full"], "ip": snap["reject_ip"],
                    "bad": snap["reject_bad"]},
        "limits": {"max_rooms": MAX_ROOMS, "max_per_ip": MAX_PER_IP,
                   "seats": MAX_RELAY_GUESTS},
        "nofile": _NOFILE,
        "threads": threading.active_count(),
    }


def _tail_log(name, lines):
    """读日志尾部。白名单文件名 + 最多 2000 行 / 200KB，防止被当成任意文件读取。"""
    if name not in ("supervisor.log", "lobby.log", "supervisor.log.1", "lobby.log.1"):
        return None, "bad name"
    path = os.path.join(os.path.dirname(DATA) or ".", name)
    if not os.path.isfile(path):
        return None, "not found"
    lines = max(1, min(ADMIN_LOG_LINES_MAX, int(lines)))
    try:
        size = os.path.getsize(path)
        block = min(size, 200 * 1024)
        with open(path, "rb") as f:
            f.seek(size - block)
            text = f.read().decode("utf-8", "replace")
    except OSError as e:
        return None, str(e)[:120]
    return "\n".join(text.splitlines()[-lines:]), ""


def _now():
    return time.time()


def _prune_locked():
    """删除 TTL 过期的房间，返回删除数。调用方须持锁。"""
    cutoff = _now() - TTL
    dead = [c for c, r in _rooms.items() if r["updated"] < cutoff]
    for c in dead:
        del _rooms[c]
    return len(dead)


def _new_code_locked():
    for _ in range(64):
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LEN))
        if code not in _rooms:
            return code
    raise RuntimeError("code space exhausted")


def _save_locked():
    """原子写盘；写失败只记日志，不影响内存中的大厅。"""
    tmp = DATA + ".tmp"
    try:
        os.makedirs(os.path.dirname(DATA), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "rooms": _rooms}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA)
    except Exception as e:
        print("save failed:", e, flush=True)


def _load():
    global _rooms
    try:
        with open(DATA, encoding="utf-8") as f:
            blob = json.load(f)
        raw = blob.get("rooms", {}) if isinstance(blob, dict) else {}
        if isinstance(raw, dict):
            _rooms = {c: r for c, r in raw.items()
                      if isinstance(r, dict) and r.get("updated", 0) >= _now() - TTL}
    except FileNotFoundError:
        pass
    except Exception as e:
        print("load failed:", e, flush=True)


def _clean_name(value):
    if not isinstance(value, str):
        return ""
    name = "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()
    return name[:MAX_NAME]


def _valid_port(value):
    return isinstance(value, int) and 1 <= value <= 65535


def _clean_steam(value):
    """房主 SteamID（17 位数字）。留空表示只能 TCP 直连。

    有这个字段，客人拿到房间就能走 Steam P2P：Steam 自己打洞，打不通走 Valve 中继，
    房主不需要在路由器上开端口。盗版/离线启动没有 Steam，字段为空，仍走 TCP。
    """
    if not isinstance(value, str):
        return ""
    s = value.strip()
    if not s.isdigit() or not (15 <= len(s) <= 20):
        return ""
    return s


def _clean_relay(value):
    """房主的中转码（6 位、与房间号同一套字符表）。留空表示不开中继。

    有它，四个人以内的房间可以在没有 Steam、也不开端口的情况下直连：数据一律经
    这台机器转发。代价是多绕一程，且本机下行 8Mbps 只够 4 个人。
    """
    if not isinstance(value, str):
        return ""
    code = value.strip().upper()
    if len(code) != CODE_LEN or any(ch not in CODE_CHARS for ch in code):
        return ""
    return code


class RelayPeer:
    """一条隧道端点。房主预留 3 条，每条对应一个座位；客人连上来按顺序配对。"""

    __slots__ = ("conn", "addr", "role", "room", "paired", "ready", "write_lock", "partner")

    def __init__(self, conn, addr, role, room):
        self.conn = conn
        self.addr = addr
        self.role = role
        self.room = room
        self.paired = False
        self.ready = threading.Event()
        self.write_lock = threading.Lock()
        self.partner = None

    def send(self, data):
        try:
            with self.write_lock:
                self.conn.sendall(data)
        except Exception:
            pass

    def close_quiet(self):
        # shutdown 唤醒另一端阻塞中的 recv，close 才真正生效；只 close 的话，
        # 正在 recv 的线程在 Linux 上不一定醒，客人会一直挂着等房主。
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    def wait_pairing(self, timeout=300):
        """房主侧：等一个客人来配对。返回 False 表示超时，或者房主已经走了。

        只等 Event 是不够的：房主在客人来之前关掉游戏时，socket 已经断了，
        但 Event 永远不会被 set，这个座位就会一直占着到超时为止——
        表现为「房主早关了，客人还能 200 连进去然后卡死」。所以每秒顺手用
        MSG_PEEK 看一眼：可读且读到 EOF 就是房主走了，立刻释放座位。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ready.wait(1.0):
                return self.paired
            try:
                ready, _, _ = select.select([self.conn], [], [], 0)
            except Exception:
                return False
            if not ready:
                continue
            try:
                peek = self.conn.recv(1, socket.MSG_PEEK)
            except Exception:
                return False
            if not peek:
                return False      # 对端关闭：别把座位留给一个已经不在的人
        return self.paired

    def leave(self):
        self.close_quiet()
        with _relay_lock:
            st = _relay_rooms.get(self.room)
            if st is None:
                return
            for i in range(len(st["hosts"])):
                if st["hosts"][i] is self:
                    st["hosts"][i] = None
            st["pairs"] = [(h, g) for (h, g) in st["pairs"]
                           if h is not self and g is not self]
            if all(h is None for h in st["hosts"]) and not st["pairs"]:
                _relay_rooms.pop(self.room, None)
                print("[relay] room %s closed" % self.room, flush=True)


def _relay_pump(src, dst):
    """单向搬运，任一端断开就把两端一起关掉。跑在 do_CONNECT 的线程里，
    所以 handler 不会返回、socket 不会被 http.server 的 finish() 关掉。"""
    try:
        while True:
            data = src.conn.recv(65536)
            if not data:
                break
            _note_bytes(len(data))
            dst.send(data)
    except Exception:
        pass
    finally:
        src.close_quiet()
        dst.close_quiet()
        src.leave()
        dst.leave()


def _relay_pair(host_peer, guest_peer):
    host_peer.partner = guest_peer
    guest_peer.partner = host_peer
    host_peer.paired = True
    guest_peer.paired = True
    host_peer.ready.set()


class Handler(BaseHTTPRequestHandler):
    server_version = "IFZLobby/1.0"
    protocol_version = "HTTP/1.1"

    # ---- helpers ----
    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_raw(self, body, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % filename)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _admin_api(self, path, q):
        """面板的数据端点。全部返回 JSON，前端只做渲染。"""
        name = path[len("/admin/api/"):]
        if name == "overview":
            days = []
            if os.path.isdir(TELEMETRY_DIR):
                days = sorted([d for d in os.listdir(TELEMETRY_DIR)
                               if os.path.isdir(os.path.join(TELEMETRY_DIR, d))],
                              reverse=True)[:30]
            with _tel_lock:
                snap = dict(_tel_stats)
                for k in ("sessions",):
                    snap[k] = len(snap.get(k) or [])
                snap["pending"] = 0
            summary = _tel_summary(1)
            return self._send({
                "ok": True,
                "host": socket.gethostname(),
                "version": VERSION,
                "metrics": _metrics_snapshot(),
                "telstats": snap,
                "summary": summary,
                "days": days,
            })
        if name == "rooms":
            now = _now()
            with _lock:
                _prune_locked()
                rows = []
                for code, r in _rooms.items():
                    rows.append({
                        "code": code, "name": r.get("name", ""),
                        "city": r.get("city", ""), "ip": r.get("ip", ""),
                        "port": r.get("port"), "players": r.get("players", 1),
                        "relay": r.get("relay", ""),
                        "age": int(now - r.get("updated", now)),
                    })
            rows.sort(key=lambda x: x["age"])
            return self._send({"ok": True, "rooms": rows})
        if name == "sessions":
            day = (q.get("day", [""])[0] or "").strip()
            if len(day) != 10 or not day.replace("-", "").isdigit():
                return self._send({"ok": False, "error": "bad day"}, 400)
            return self._send({"ok": True, "day": day, "sessions": _admin_sessions(day)})
        if name == "session":
            day = (q.get("day", [""])[0] or "").strip()
            sid = (q.get("id", [""])[0] or "").strip()
            if len(day) != 10 or _tel_safe_session(sid) is None:
                return self._send({"ok": False, "error": "bad args"}, 400)
            return self._send(_tel_session(day, sid))
        if name == "logs":
            text, err = _tail_log((q.get("name", [""])[0] or ""),
                                  (q.get("lines", ["200"])[0] or "200"))
            if text is None:
                return self._send({"ok": False, "error": err}, 404)
            if (q.get("download", [""])[0] or "") == "1":
                data = text.encode("utf-8")
                return self._send_raw(data, "text/plain; charset=utf-8",
                                      filename=(q.get("name", ["log"])[0] or "log") + ".txt")
            return self._send({"ok": True, "text": text})
        return self._send({"ok": False, "error": "unknown"}, 404)

    def _telemetry(self):
        """POST /api/telemetry。

        单独一条读取路径：通用 _body() 的上限是 8KB，遥测一批最多 160KB。
        校验从严——session 进文件名、events 进 JSON 解析、IP 二次脱敏。
        """
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0:
            return self._send({"ok": False, "error": "empty body"}, 400)
        if n > TELEMETRY_MAX_BODY:
            _tel_stats["rejects"]["size"] += 1
            return self._send({"ok": False, "error": "too large"}, 413)

        ip = self._origin_ip({})
        if not _tel_allow(ip):
            return self._send({"ok": False, "error": "slow down"}, 429)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            _tel_stats["rejects"]["bad"] += 1
            return self._send({"ok": False, "error": "bad json"}, 400)
        if not isinstance(body, dict):
            _tel_stats["rejects"]["bad"] += 1
            return self._send({"ok": False, "error": "bad payload"}, 400)

        session = _tel_safe_session(body.get("session"))
        if session is None:
            _tel_stats["rejects"]["bad"] += 1
            return self._send({"ok": False, "error": "bad session"}, 400)
        events = body.get("events")
        if not isinstance(events, list) or len(events) > 2000:
            _tel_stats["rejects"]["bad"] += 1
            return self._send({"ok": False, "error": "bad events"}, 400)

        # 二次脱敏：客户端已经擦过一遍，但服务器不能假设客户端可信。
        body["ip"] = _tel_mask_ip(ip)
        body["recv"] = _now()
        _tel_store(session, body)
        _tel_prune()
        return self._send({"ok": True, "stored": len(events)})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if n <= 0 or n > 8192:
            return {}
        try:
            blob = json.loads(self.rfile.read(n).decode("utf-8"))
            return blob if isinstance(blob, dict) else {}
        except Exception:
            return {}

    def _origin_ip(self, body):
        """房主公网 IP：优先 Cloudflare 透传的真实访客 IP，否则用来源地址。

        走 Cloudflare Tunnel 时 client_address 是本地回环（隧道自己连过来的），
        必须读 CF-Connecting-IP，否则房间会登记成 127.0.0.1，客人根本连不上。
        """
        for head in ("CF-Connecting-IP", "X-Forwarded-For"):
            raw = self.headers.get(head, "")
            if not raw:
                continue
            candidate = raw.split(",")[0].strip()
            if candidate and all(c.isdigit() or c == "." for c in candidate):
                return candidate[:64]
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1") and isinstance(body.get("ip"), str):
            return body["ip"].strip()[:64]
        return ip

    def log_message(self, fmt, *args):
        # 精简访问日志，避免心跳把磁盘写爆
        line = fmt % args
        if "/heartbeat" in line:
            return
        print("[lobby] " + line, flush=True)

    # ---- routes ----
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        # ---- 运维面板 ----
        q = parse_qs(u.query)
        if path in ("/admin", "/admin/"):
            # 页面本身是登录框，不含任何数据，可以公开；数据接口一律要令牌。
            if not _admin_token():
                return self._send({"ok": False, "error": "admin disabled",
                                   "hint": "set IFZ_ADMIN_TOKEN or create admin.token"}, 403)
            return self._send_raw(ADMIN_HTML.encode("utf-8"),
                                  "text/html; charset=utf-8")
        if path.startswith("/admin/api/"):
            if not _admin_ok(self.headers, q):
                return self._send({"ok": False, "error": "unauthorized"}, 401)
            return self._admin_api(path, q)
        if path == "/api/telemetry/download":
            if not _admin_ok(self.headers, q):
                return self._send({"ok": False, "error": "unauthorized"}, 401)
            day = (q.get("day", [""])[0] or "").strip()
            sid = (q.get("session", [""])[0] or "").strip()
            if day and (len(day) != 10 or not day.replace("-", "").isdigit()):
                return self._send({"ok": False, "error": "bad day"}, 400)
            data, err = _tel_zip(day, sid)
            if data is None:
                return self._send({"ok": False, "error": err}, 404)
            name = (sid + ".jsonl.zip") if sid else ("ifz-telemetry-%s.zip" % (day or "all"))
            return self._send_raw(data, "application/zip", filename=name)

        if path == "/health":
            with _lock:
                _prune_locked()
            rooms, relay_rooms, pairs, _free = _gauge()
            return self._send({"ok": True, "version": VERSION, "rooms": rooms,
                               "relay_rooms": relay_rooms, "pairs": pairs,
                               "ttl": TTL, "max_rooms": MAX_ROOMS,
                               "servers": len(_servers_list())})
        if path == "/api/servers":
            # 客户端靠这一句知道现在有哪些大厅。内容是服务器自己说了算的，
            # 客户端只做「是不是 http/https、域名是不是我们家的」这类校验。
            return self._send({"ok": True, "version": VERSION,
                               "servers": _servers_list(),
                               "ttl": SERVER_LIST_TTL})
        if path == "/metrics":
            return self._send(_metrics_snapshot())
        if path == "/api/telemetry/summary":
            days = parse_qs(u.query).get("days", ["7"])[0]
            try:
                days = max(1, min(30, int(days)))
            except ValueError:
                days = 7
            return self._send(_tel_summary(days))
        if path == "/api/telemetry/stats":
            with _tel_lock:
                snap = dict(_tel_stats)
                snap["sessions"] = len(snap["sessions"])
                snap["dir"] = TELEMETRY_DIR
            return self._send({"ok": True, "version": VERSION,
                               "today": _tel_day(0), **snap})
        if path.startswith("/api/telemetry/"):
            session = _tel_safe_session(path.rsplit("/", 1)[-1])
            if session is None:
                return self._send({"ok": False, "error": "bad session"}, 400)
            day = parse_qs(u.query).get("day", [""])[0] or _tel_day(0)
            if len(day) != 10 or not day.replace("-", "").isdigit():
                return self._send({"ok": False, "error": "bad day"}, 400)
            return self._send(_tel_session(day, session))
        if path == "/api/relay":
            # 客人选台用：这个房间在本台还有几个空座位。只读，不加锁住状态。
            room = (parse_qs(u.query).get("room", [""])[0] or "").strip().upper()
            if _clean_relay(room) != room:
                return self._send({"ok": False, "error": "bad room code"}, 400)
            with _relay_lock:
                st = _relay_rooms.get(room)
                if st is None:
                    hosts = 0
                    guests = 0
                    free = 0
                else:
                    hosts = sum(1 for h in st["hosts"] if h is not None)
                    guests = len(st["pairs"])
                    free = sum(1 for h in st["hosts"]
                               if h is not None and not h.paired)
            return self._send({"ok": True, "room": room, "hosts": hosts,
                               "guests": guests, "free": free,
                               "seats": MAX_RELAY_GUESTS})
        if path == "/api/rooms":
            q = (parse_qs(u.query).get("q", [""])[0] or "").strip().lower()
            with _lock:
                _prune_locked()
                rows = []
                for code, r in _rooms.items():
                    if q and q not in code.lower() and q not in r["name"].lower():
                        continue
                    rows.append({
                        "code": code,
                        "name": r["name"],
                        "ip": r["ip"],
                        "port": r["port"],
                        "city": r.get("city", ""),
                        "steam": r.get("steam", ""),
                        "relay": r.get("relay", ""),
                        "players": r.get("players", 1),
                        "age": int(_now() - r["updated"]),
                    })
                rows.sort(key=lambda x: (-x["players"], x["name"]))
                return self._send({"ok": True, "count": len(rows), "rooms": rows})
        return self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path

        # 遥测要单独读：通用 _body() 只收 8KB，一批事件最多 160KB。
        if path == "/api/telemetry":
            return self._telemetry()

        body = self._body()

        if path == "/api/room":
            name = _clean_name(body.get("name"))
            port = body.get("port")
            if not name:
                _count("reject_bad")
                return self._send({"ok": False, "error": "empty room name"}, 400)
            if not _valid_port(port):
                _count("reject_bad")
                return self._send({"ok": False, "error": "bad port"}, 400)
            ip = self._origin_ip(body)
            with _lock:
                _prune_locked()
                mine = [c for c, r in _rooms.items() if r["ip"] == ip]
                if len(_rooms) >= MAX_ROOMS:
                    _count("reject_full")
                    return self._send({"ok": False, "error": "lobby full"}, 503)
                if len(mine) >= MAX_PER_IP:
                    _count("reject_ip")
                    return self._send({"ok": False, "error": "too many rooms from this ip"}, 429)
                code = _new_code_locked()
                token = secrets.token_hex(16)
                _rooms[code] = {
                    "name": name,
                    "token": token,
                    "ip": ip,
                    "port": port,
                    "city": _clean_name(body.get("city"))[:24],
                    "steam": _clean_steam(body.get("steam")),
                    "relay": _clean_relay(body.get("relay")),
                    "players": body.get("players") if isinstance(body.get("players"), int) else 1,
                    "updated": _now(),
                }
                _save_locked()
            _count("registrations")
            print("[lobby] register " + code + " " + name + " " + ip + ":" + str(port), flush=True)
            return self._send({"ok": True, "code": code, "token": token,
                               "ip": ip, "port": port, "ttl": TTL})

        if path.startswith("/api/room/") and path.endswith("/heartbeat"):
            code = unquote(path[len("/api/room/"):-len("/heartbeat")]).strip().upper()
            with _lock:
                r = _rooms.get(code)
                if r is None:
                    return self._send({"ok": False, "error": "no such room"}, 404)
                if body.get("token") != r["token"]:
                    return self._send({"ok": False, "error": "bad token"}, 403)
                r["updated"] = _now()
                if isinstance(body.get("players"), int) and 0 <= body["players"] <= 32:
                    r["players"] = body["players"]
                if isinstance(body.get("city"), str):
                    r["city"] = _clean_name(body["city"])[:24]
            return self._send({"ok": True, "code": code})

        return self._send({"ok": False, "error": "not found"}, 404)

    def do_CONNECT(self):
        # 游戏数据的中转隧道。HTTP CONNECT 只是握手，之后这条 socket 就是裸 TCP。
        # 房主开房时按 MAX_RELAY_GUESTS 预留一组（两台一起跑就是两组），
        # 客人按顺序和未配对的座位配对，一条隧道只服务一个人。
        u = urlparse(self.path)
        if u.path != "/relay":
            self.send_error(404, "no tunnel here")
            return
        qs = parse_qs(u.query)
        room = (qs.get("room", [""])[0] or "").strip().upper()
        role = (qs.get("role", [""])[0] or "").strip().lower()
        if _clean_relay(room) != room:
            self.send_error(400, "bad room code")
            return
        if role not in ("host", "guest"):
            self.send_error(400, "bad role")
            return
        with _relay_lock:
            st = _relay_rooms.get(room)
            if role == "host":
                if st is None:
                    st = {"hosts": [None] * MAX_RELAY_GUESTS,
                          "pairs": [], "created": _now()}
                    _relay_rooms[room] = st
                if all(h is not None for h in st["hosts"]):
                    self.send_error(409, "no free host slot")
                    return
            else:
                if st is None:
                    self.send_error(404, "room has no host")
                    return
                if not any(h is not None and not h.paired for h in st["hosts"]):
                    self.send_error(404, "no host waiting")
                    return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.wfile.flush()
        peer = RelayPeer(self.connection, self.client_address, role, room)
        _count("tunnels")
        if role == "host":
            with _relay_lock:
                st = _relay_rooms.get(room)
                if st is None:
                    peer.close_quiet()
                    return
                slot = -1
                for i in range(len(st["hosts"])):
                    if st["hosts"][i] is None:
                        slot = i
                        break
                if slot < 0:
                    peer.close_quiet()
                    return
                st["hosts"][slot] = peer
            print("[relay] host slot %d ready in %s from %s"
                  % (slot, room, self.client_address[0]), flush=True)
            if not peer.wait_pairing():
                peer.leave()
                return
            _relay_pump(peer, peer.partner)
            return
        with _relay_lock:
            st = _relay_rooms.get(room)
            host_peer = None
            slot = -1
            if st is not None:
                for i in range(len(st["hosts"])):
                    h = st["hosts"][i]
                    if h is not None and not h.paired:
                        host_peer = h
                        slot = i
                        break
            if host_peer is None:
                peer.close_quiet()
                return
            st["pairs"].append((host_peer, peer))
        _count("pairs")
        _relay_pair(host_peer, peer)
        print("[relay] guest paired slot %d in %s from %s"
              % (slot, room, self.client_address[0]), flush=True)
        _relay_pump(peer, peer.partner)

    def do_DELETE(self):
        u = urlparse(self.path)
        path = u.path
        if not path.startswith("/api/room/"):
            return self._send({"ok": False, "error": "not found"}, 404)
        code = unquote(path[len("/api/room/"):]).strip().upper()
        token = (parse_qs(u.query).get("token", [""])[0]
                 or self.headers.get("X-IFZ-Token", ""))
        with _lock:
            r = _rooms.get(code)
            if r is None:
                return self._send({"ok": False, "error": "no such room"}, 404)
            if token != r["token"]:
                return self._send({"ok": False, "error": "bad token"}, 403)
            del _rooms[code]
            _save_locked()
        print("[lobby] unregister " + code, flush=True)
        return self._send({"ok": True, "code": code})


HANDOFF_MARK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".supervisor-handoff")


def _handoff_to_new_supervisor():
    """从 v1 守护平滑迁移到 v2（只会在升级的那一瞬间命中一次）。

    v1 的入口不会重启自己，所以换了 supervisor.py 也得等面板重启才生效。
    这里做一次交接：新大厅进程发现「自己是被老守护拉起来的」（环境里没有
    IFZ_SUPERVISOR_VERSION=2），而磁盘上的 supervisor.py 已经是 v2，就让
    老守护退出、自己也退出。面板 daemon 看到主进程结束会重启容器，届时
    新 entry + 新守护一起加载。

    干净环境里 main.py 已经把变量设成 2，所以永远不会走到这里。
    """
    if os.environ.get("IFZ_SUPERVISOR_VERSION") == "2":
        return
    if os.environ.get("IFZ_AUTO_HANDOFF", "1") == "0":
        return
    sup = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy", "supervisor.py")
    try:
        with open(sup, "rb") as f:
            blob = f.read()
    except Exception:
        return
    if b'SUPERVISOR_VERSION = "2"' not in blob:
        return
    try:
        if os.path.exists(HANDOFF_MARK):
            return          # 已经交接过，别反复折腾
        with open(HANDOFF_MARK, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass
    ppid = os.getppid()
    print("[lobby] old supervisor detected (v1); handing over to v2: "
          "parent pid=%d will be stopped, panel restarts the container" % ppid, flush=True)
    try:
        if ppid > 1:
            os.kill(ppid, signal.SIGTERM)
    except Exception as e:
        print("[lobby] cannot signal parent: %s" % e, flush=True)
    time.sleep(1.0)
    os._exit(0)


def main():
    _handoff_to_new_supervisor()
    _load()
    global _NOFILE
    _NOFILE = _raise_nofile()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("IFZ lobby %s listening on 0.0.0.0:%d | ttl=%ds | rooms<=%d | per-ip<=%d | seats=%d | nofile=%s | data=%s"
          % (VERSION, PORT, TTL, MAX_ROOMS, MAX_PER_IP, MAX_RELAY_GUESTS, _NOFILE, DATA), flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
