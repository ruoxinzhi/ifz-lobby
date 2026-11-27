#!/usr/bin/env python3
"""IFZ 联机大厅（房间目录服务）。

职责只有一个：保存「房间名 / 房间号 -> 房主地址:端口」的映射，让玩家
在主菜单就能搜到朋友开的房。**不中转游戏数据**：搜索完成后客人仍然用
现有的 TCP 直连房主，服务器 347ms 的延迟只影响查房间，不影响战斗手感。

纯 Python 标准库，无第三方依赖。接口：

    GET  /health                       -> {"ok":true,"rooms":N,...}
    POST /api/room                     -> 注册，返回 {"code","token","ip","port",...}
    POST /api/room/<code>/heartbeat    -> 保活（需 token）
    DELETE /api/room/<code>?token=...  -> 注销（需 token）
    GET  /api/rooms?q=<关键词>          -> 搜索（按房间号或房间名，包含匹配）

安全边界：
  - 房主 IP 取 TCP 连接的来源地址，客户端不能伪造（除非来源是本机，仅测试用）。
  - token 是 32 位随机 hex，只有持有者能保活/注销，防止别人踢掉房间。
  - TTL 过期自动清理；房间数、单 IP 房间数、房间名长度都有限制。
"""
import json
import os
import secrets
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

PORT = int(os.environ.get("IFZ_LOBBY_PORT", "8080"))
_NOFILE = -1
# 网页面板没有命令行时，靠这一行确认「拉到的确实是新版」：浏览器打开 /health 就能看到。
VERSION = "2026-11-28.1"
DATA = os.environ.get("IFZ_LOBBY_DATA", "/root/ifz-lobby/rooms.json")
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
        if path == "/health":
            with _lock:
                _prune_locked()
            rooms, relay_rooms, pairs, _free = _gauge()
            return self._send({"ok": True, "version": VERSION, "rooms": rooms,
                               "relay_rooms": relay_rooms, "pairs": pairs,
                               "ttl": TTL, "max_rooms": MAX_ROOMS})
        if path == "/metrics":
            with _lock:
                _prune_locked()
            rooms, relay_rooms, pairs, free = _gauge()
            mbps = _mbps_since_last()
            with _stats_lock:
                snap = dict(_stats)
                uptime = int(time.time() - snap["since"])
            return self._send({
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
            })
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


def main():
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
