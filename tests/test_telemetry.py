#!/usr/bin/env python3
"""遥测端点自测：接收、归档、汇总、拒绝路径。

不依赖外网，起一个本地实例打真实 HTTP。
用法： python tests/test_telemetry.py
"""
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ok   " if ok else "  FAIL ") + name + ((" | " + detail) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def post(url, obj, timeout=10):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}


def get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}


def payload(session, events, mod="IFZ.ModPack 1.3.0"):
    return {
        "v": 1, "session": session,
        "env": {"mod": mod, "os": "Windows 11", "game": "0.26.10",
                "lang": "Chinese", "cores": "16", "net": {"fail": 2}},
        "events": events,
    }


def main():
    tmp = tempfile.mkdtemp(prefix="ifz-tel-")
    port = free_port()
    env = dict(os.environ)
    env["IFZ_LOBBY_PORT"] = str(port)
    env["IFZ_LOBBY_DATA"] = os.path.join(tmp, "rooms.json")
    env["IFZ_TELEMETRY_DIR"] = os.path.join(tmp, "telemetry")
    env["IFZ_TELEMETRY_PER_MIN"] = "6"
    log = open(os.path.join(tmp, "lobby.log"), "wb")
    proc = subprocess.Popen([sys.executable, "lobby_server.py"], cwd=ROOT,
                            env=env, stdout=log, stderr=log)
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(60):
            try:
                get(base + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            print("server did not start")
            return 1

        # ---- 正常路径 ----
        events = [
            {"t": 0.5, "lvl": "info", "cat": "boot", "ev": "start",
             "f": {"session": "a" * 16, "enabled": True}},
            {"t": 1.2, "lvl": "info", "cat": "layer", "ev": "applied",
             "f": {"net": 12, "morale": 0, "mines": 0, "menu": 1}},
            {"t": 2.0, "lvl": "warn", "cat": "log", "ev": "warn",
             "f": {"msg": "lobby register failed at http://x"}},
            {"t": 3.0, "lvl": "error", "cat": "log", "ev": "error",
             "f": {"msg": "reflect failed"}, "err": "TargetException: boom"},
            {"t": 4.0, "lvl": "info", "cat": "net", "ev": "drop",
             "f": {"phase": "Active", "reason": "peer went silent for 12s"}},
        ]
        st, body = post(base + "/api/telemetry", payload("deadbeefdeadbeef", events))
        check("遥测接收成功", st == 200 and body.get("stored") == 5, str(body)[:120])

        day = time.strftime("%Y-%m-%d")
        path = os.path.join(tmp, "telemetry", day, "deadbeefdeadbeef.jsonl")
        check("按 天/会话 归档", os.path.isfile(path), path)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                lines = [json.loads(x) for x in f if x.strip()]
            check("归档内容完整", len(lines) == 1 and len(lines[0]["events"]) == 5)
            check("服务端补了脱敏 IP", "ip" in lines[0], str(lines[0].get("ip")))

        # ---- 汇总 ----
        st, s = get(base + "/api/telemetry/summary?days=1")
        check("汇总可读", st == 200 and s.get("sessions") == 1, str(s)[:100])
        check("汇总统计错误数", s.get("errors") == 1, "errors=%s" % s.get("errors"))
        check("汇总列出断线原因", any("Active" in k for k in s.get("drop", {})),
              str(s.get("drop"))[:120])
        check("汇总指出 0 钩子的层", "morale" in s.get("layer_zero", {})
              and "mines" in s.get("layer_zero", {}), str(s.get("layer_zero")))
        check("汇总按 mod 版本分桶", any("1.3.0" in k for k in s.get("mod", {})),
              str(s.get("mod"))[:100])

        # ---- 单会话 ----
        st, one = get(base + "/api/telemetry/deadbeefdeadbeef")
        check("单会话可回放", st == 200 and one.get("total") == 5 and one.get("env"),
              "total=%s" % one.get("total"))

        # ---- 拒绝路径 ----
        st, body = post(base + "/api/telemetry", payload("../etc/passwd", events))
        check("拒绝非法 session（防路径穿越）", st == 400, str(body)[:80])

        st, body = post(base + "/api/telemetry", {"v": 1, "session": "aabbccdd" * 2,
                                                  "events": "not-a-list"})
        check("拒绝非法 events", st == 400, str(body)[:80])

        big = payload("cafebabecafebabe", [{"x": "y" * 400}] * 900)
        st, body = post(base + "/api/telemetry", big)
        check("超大包被拒（413）", st == 413, "%s %s" % (st, str(body)[:60]))

        # 限流：前面已用 3 次，PER_MIN=6，再打 5 次应出现 429
        codes = []
        for i in range(5):
            st, _ = post(base + "/api/telemetry",
                         payload("aaaaaaaabbbbbbbb", [{"t": i, "lvl": "info",
                                                      "cat": "t", "ev": "x"}]))
            codes.append(st)
        check("单 IP 限流生效", 429 in codes, str(codes))

        # ---- 统计端点 ----
        st, stats = get(base + "/api/telemetry/stats")
        check("统计端点可用", st == 200 and stats.get("ok"), str(stats)[:100])
        check("拒绝计数有记录", stats.get("rejects", {}).get("bad", 0) >= 2,
              str(stats.get("rejects")))
    finally:
        log.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("  - " + name)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
