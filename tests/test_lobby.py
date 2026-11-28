#!/usr/bin/env python3
"""大厅服务自测：限额、指标、中转隧道。

不需要外网，也不碰线上：随机端口起一个本地实例，跑完关掉。

    python tests/test_lobby.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "lobby_server.py")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS  " if ok else "FAIL  ") + name + ((" | " + detail) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)


def post(url, body, timeout=5):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def delete(url):
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def open_tunnel(port, room, role):
    """模拟 CONNECT：返回已建立隧道的 socket。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(("CONNECT /relay?room=%s&role=%s HTTP/1.1\r\nHost: localhost\r\n\r\n"
               % (room, role)).encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(1)
        if not chunk:
            raise RuntimeError("tunnel closed during handshake")
        head += chunk
    if b" 200" not in head.split(b"\r\n")[0]:
        raise RuntimeError("tunnel refused: " + head.split(b"\r\n")[0].decode("latin1"))
    return s


def main():
    port = free_port()
    data = os.path.join(tempfile.mkdtemp(prefix="ifz-lobby-"), "rooms.json")
    env = dict(os.environ)
    env.update({"IFZ_LOBBY_PORT": str(port), "IFZ_LOBBY_DATA": data,
                "IFZ_LOBBY_MAX": "3", "IFZ_LOBBY_MAX_PER_IP": "2"})
    proc = subprocess.Popen([sys.executable, SERVER], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(50):
            try:
                get(base + "/health")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("server did not start")

        h = get(base + "/health")
        check("health reports version and rooms", h.get("ok") and "version" in h,
              json.dumps(h, ensure_ascii=False))

        m = get(base + "/metrics")
        check("metrics exposes limits and nofile",
              m.get("limits", {}).get("max_rooms") == 3
              and m.get("limits", {}).get("max_per_ip") == 2
              and "nofile" in m,
              "limits=%s nofile=%s" % (m.get("limits"), m.get("nofile")))

        # ---- 节点列表下发：客户端只内置一个引导地址，其余由这里说了算 ----
        srv = get(base + "/api/servers")
        seed_file = os.path.join(os.path.dirname(data), "servers.json")
        check("the node list is served and seeded next to the data",
              srv.get("ok") and srv.get("servers") and os.path.isfile(seed_file),
              json.dumps(srv, ensure_ascii=False))

        with open(seed_file, "w", encoding="utf-8") as f:
            json.dump({"servers": ["http://ok.nodaroo.fun:1", "ftp://bad", 42]}, f)
        srv2 = get(base + "/api/servers")
        check("editing servers.json takes effect without a restart",
              srv2.get("servers") == ["http://ok.nodaroo.fun:1"], str(srv2.get("servers")))

        with open(seed_file, "w", encoding="utf-8") as f:
            f.write("{broken")
        srv3 = get(base + "/api/servers")
        check("a corrupt node list still answers with the built-in seed",
              len(srv3.get("servers") or []) >= 1, str(srv3.get("servers")))

        codes = []
        for i in range(2):
            st, body = post(base + "/api/room", {"name": "room-%d" % i, "port": 7777})
            check("register #%d accepted" % (i + 1), st == 200 and body.get("ok"),
                  str(st))
            if body.get("ok"):
                codes.append(body)
        st, body = post(base + "/api/room", {"name": "room-3", "port": 7777})
        check("the per-IP cap refuses a third room", st == 429 and not body.get("ok"),
              "status=%s error=%s" % (st, body.get("error")))

        m = get(base + "/metrics")
        check("the rejection is counted", m.get("rejects", {}).get("ip") == 1,
              str(m.get("rejects")))

        st, _ = delete(base + "/api/room/%s?token=%s" % (codes[0]["code"], codes[0]["token"]))
        st2, body2 = post(base + "/api/room", {"name": "room-again", "port": 7777})
        check("freeing a room frees the IP slot", st == 200 and st2 == 200, "status=%s" % st2)

        # ---- 中转隧道：房主一条、客人一条，数据能过去 ----
        room = codes[1]["code"]
        host = open_tunnel(port, room, "host")
        guest = open_tunnel(port, room, "guest")
        time.sleep(0.2)
        payload = b"hello-relay" * 100
        host.sendall(payload)
        got = b""
        guest.settimeout(5)
        while len(got) < len(payload):
            chunk = guest.recv(65536)
            if not chunk:
                break
            got += chunk
        check("data flows through the relay tunnel", got == payload,
              "%d/%d bytes" % (len(got), len(payload)))

        m = get(base + "/metrics")
        check("metrics counts tunnels, pairs and bytes",
              m.get("tunnels_opened", 0) >= 2 and m.get("pairs_made", 0) >= 1
              and m.get("bytes_relayed", 0) >= len(payload),
              "tunnels=%s pairs=%s bytes=%s" % (m.get("tunnels_opened"),
                                                m.get("pairs_made"), m.get("bytes_relayed")))
        check("live gauges show the paired guest",
              m.get("relay_rooms") == 1 and m.get("pairs") == 1,
              "rooms=%s pairs=%s" % (m.get("relay_rooms"), m.get("pairs")))

        host.close()
        guest.close()
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0].decode("utf-8", "replace")
        except Exception:
            proc.kill()
            out = ""
        if FAIL:
            print("--- server log ---")
            print(out[-2000:])

    print("\nLOBBY TESTS: %s (%d passed, %d failed)"
          % ("PASS" if not FAIL else "FAIL", len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
