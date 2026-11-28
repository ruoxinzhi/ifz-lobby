#!/usr/bin/env python3
"""运维面板自测：鉴权、聚合、房间、会话、日志、下载。

用法： python tests/test_admin.py
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOKEN = "test-token-8f3a"
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ok   " if ok else "  FAIL ") + name + ((" | " + detail) if detail else ""))


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p


def get(url, token=None, raw=False):
    headers = {}
    if token:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            if raw:
                return r.status, body
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, {"_raw": body[:200].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main():
    tmp = tempfile.mkdtemp(prefix="ifz-admin-")
    port = free_port()
    env = dict(os.environ)
    env["IFZ_LOBBY_PORT"] = str(port)
    env["IFZ_LOBBY_DATA"] = os.path.join(tmp, "rooms.json")
    env["IFZ_TELEMETRY_DIR"] = os.path.join(tmp, "telemetry")
    env["IFZ_ADMIN_TOKEN"] = TOKEN
    # lobby.log 平时由 supervisor 的 Child.start 创建；这里只起 lobby_server，
    # 自己造一份才能测「读日志尾部」这条路径。
    with open(os.path.join(tmp, "lobby.log"), "w", encoding="utf-8") as f:
        for i in range(30):
            f.write("[lobby] test line %d\n" % i)
    log = open(os.path.join(tmp, "server.log"), "wb")
    proc = subprocess.Popen([sys.executable, "lobby_server.py"], cwd=ROOT,
                            env=env, stdout=log, stderr=log)
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(60):
            try:
                get(base + "/health"); break
            except Exception:
                time.sleep(0.2)
        else:
            print("server did not start"); return 1

        # ---- 鉴权 ----
        st, d = get(base + "/admin")
        check("面板页面对未登录者也返回登录框", st == 200 and "_raw" in d or st == 200,
              "status=%s" % st)
        st, body = get(base + "/admin?token=" + TOKEN, raw=True)
        check("带令牌返回 HTML",
              st == 200 and b"<!doctype" in body.lower() and b"IFZ" in body,
              "%s bytes" % len(body if isinstance(body, bytes) else b""))
        st, _ = get(base + "/admin/api/overview")
        check("接口无令牌返回 401", st == 401, "status=%s" % st)

        # ---- 房间 ----
        post(base + "/api/room", {"name": "运维测试房", "port": 7777, "city": "灰堡"})
        st, d = get(base + "/admin/api/rooms", TOKEN)
        check("房间列表可读", st == 200 and len(d.get("rooms", [])) == 1,
              str(d.get("rooms"))[:140])
        if d.get("rooms"):
            r = d["rooms"][0]
            check("房间字段齐全", all(k in r for k in
                  ("code", "name", "city", "ip", "port", "players", "age")),
                  str(sorted(r.keys())))
            # 管理面板要能看出房主在哪，这里的 IP 是给人看的，不能脱敏
            check("房间 IP 是真实地址（运维需要）",
                  r["ip"].count(".") == 3 and all(p.isdigit() for p in r["ip"].split(".")),
                  r["ip"])

        # ---- 概览 ----
        st, d = get(base + "/admin/api/overview", TOKEN)
        check("概览聚合成功", st == 200 and d.get("metrics", {}).get("ok"),
              str(d.get("version")))
        check("概览带上了域名与天数", "host" in d and isinstance(d.get("days"), list),
              "host=%s days=%s" % (d.get("host"), d.get("days")))

        # ---- 遥测归档 + 会话 ----
        day = time.strftime("%Y-%m-%d")
        session = "abc123def456"
        events = [
            {"t": 0.5, "lvl": "info", "cat": "boot", "ev": "start", "f": {"enabled": True}},
            {"t": 3.0, "lvl": "info", "cat": "layer", "ev": "applied",
             "f": {"net": 12, "morale": 0}},
            {"t": 9.0, "lvl": "info", "cat": "net", "ev": "drop",
             "f": {"phase": "Handshaking", "reason": "version mismatch"}},
            {"t": 9.5, "lvl": "error", "cat": "log", "ev": "error",
             "f": {"msg": "boom"}, "err": "X: y"},
        ]
        post(base + "/api/telemetry", {
            "v": 1, "session": session,
            "env": {"mod": "IFZ.ModPack 1.3.0", "os": "Windows 11", "game": "0.26.10",
                    "net": {"direct": 2, "relay": 1, "fail": 1}},
            "events": events,
        })
        st, d = get(base + "/admin/api/sessions?day=" + day, TOKEN)
        check("会话列表可读", st == 200 and len(d.get("sessions", [])) == 1,
              str(d.get("sessions"))[:160])
        if d.get("sessions"):
            s = d["sessions"][0]
            check("会话统计正确", s["events"] == 4 and s["errors"] == 1,
                  "events=%s errors=%s" % (s["events"], s["errors"]))

        st, d = get(base + "/admin/api/session?day=%s&id=%s" % (day, session), TOKEN)
        check("单会话可回放", st == 200 and d.get("total") == 4 and d.get("env"),
              "total=%s" % d.get("total"))

        # 链路计数进汇总
        st, d = get(base + "/admin/api/overview", TOKEN)
        check("概览汇总了链路计数",
              d.get("summary", {}).get("link_direct") == 2
              and d.get("summary", {}).get("link_relay") == 1,
              str({k: v for k, v in d.get("summary", {}).items() if k.startswith("link_")}))

        # ---- 下载 ----
        st, body = get(base + "/api/telemetry/download?day=" + day, TOKEN, raw=True)
        ok_zip = False
        names = []
        if st == 200 and isinstance(body, bytes):
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as z:
                    names = z.namelist()
                    ok_zip = any(n.endswith(session + ".jsonl") for n in names)
            except Exception:
                ok_zip = False
        check("整天下载是合法 zip 且含会话", ok_zip, str(names)[:120])

        st, body = get(base + "/api/telemetry/download?day=%s&session=%s" % (day, session),
                       TOKEN, raw=True)
        one_ok = False
        if st == 200 and isinstance(body, bytes):
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as z:
                    one_ok = z.namelist() == [session + ".jsonl"]
            except Exception:
                pass
        check("单会话下载只含一个文件", one_ok)

        st, _ = get(base + "/api/telemetry/download?day=" + day)
        check("下载也要令牌", st == 401, "status=%s" % st)

        # ---- 日志 ----
        st, d = get(base + "/admin/api/logs?name=lobby.log&lines=50", TOKEN)
        check("日志可读", st == 200 and isinstance(d.get("text"), str),
              "%d chars" % len(d.get("text") or ""))
        st, d = get(base + "/admin/api/logs?name=../../etc/passwd&lines=10", TOKEN)
        check("拒绝任意文件读取", st == 404, str(d)[:80])

        st, body = get(base + "/admin/api/logs?name=lobby.log&lines=20&download=1",
                       TOKEN, raw=True)
        check("日志可下载", st == 200 and isinstance(body, bytes), "%s bytes" % (
            len(body) if isinstance(body, bytes) else 0))
    finally:
        log.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    for n in FAIL:
        print("  - " + n)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
