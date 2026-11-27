#!/usr/bin/env python3
"""保活三层自测（不依赖外网、不影响线上）。

覆盖：
  1. 正常启动能返回 /health
  2. 杀掉大厅子进程 -> 守护自动拉起，端口恢复
  3. 杀掉守护进程   -> 入口自动拉起，端口恢复
  4. 重复启动守护   -> 单实例锁挡住（无 fcntl 的平台跳过）

用法： python tests/test_keepalive.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = int(os.environ.get("IFZ_TEST_PORT", "18089"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ok   " if ok else "  FAIL ") + name + ((" | " + detail) if detail else ""))


def health(timeout=2.0):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def wait_health(deadline):
    while time.time() < deadline:
        h = health()
        if h and h.get("ok"):
            return h
        time.sleep(0.5)
    return None


def heartbeat(root):
    try:
        with open(os.path.join(root, ".supervisor.heartbeat"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def kill(pid, verbose=False):
    if not pid or pid <= 0:
        return
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, errors="replace")
        if verbose:
            print("    taskkill %s -> rc=%s %s" % (pid, r.returncode,
                  (r.stdout or r.stderr or "").strip()[:100]))
    else:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def supervisor_module():
    """只加载函数，不启动任何进程。"""
    import importlib.util
    path = os.path.join(ROOT, "deploy", "supervisor.py")
    spec = importlib.util.spec_from_file_location("ifz_supervisor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    sup = supervisor_module()
    check("更旧的 CDN 副本被判为降级", sup.is_downgrade("2026-11-27.2", "2026-11-28.2"),
          "jsDelivr 曾经在限流后把线上改回旧版")
    check("更高版本不算降级", not sup.is_downgrade("2026-11-28.3", "2026-11-28.2"))
    check("同版本不算降级", not sup.is_downgrade("2026-11-28.2", "2026-11-28.2"),
          "同版本改 bug 仍应能部署")
    check("版本号格式不认识时不拦", not sup.is_downgrade("weird", "2026-11-28.2"))

    tmp = tempfile.mkdtemp(prefix="ifz-keepalive-")
    try:
        for rel in ("main.py", "lobby_server.py", os.path.join("deploy", "supervisor.py")):
            src = os.path.join(ROOT, rel)
            dst = os.path.join(tmp, rel)
            parent = os.path.dirname(dst)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            shutil.copy2(src, dst)

        env = dict(os.environ)
        env["IFZ_LOBBY_PORT"] = str(PORT)
        env["PORT"] = str(PORT)
        env["IFZ_AUTOUPDATE_SECS"] = "0"      # 自测不碰外网
        env["IFZ_LOBBY_DATA"] = os.path.join(tmp, "rooms.json")

        log = open(os.path.join(tmp, "boot.log"), "wb")
        entry = subprocess.Popen([sys.executable, "main.py"], cwd=tmp, env=env,
                                 stdout=log, stderr=log)
        try:
            h = wait_health(time.time() + 25)
            check("启动后 /health 可用", bool(h), (h or {}).get("version", "no response"))
            if not h:
                return 1

            hb = heartbeat(tmp)
            child_pid = hb.get("child_pid", -1)
            sup_pid = hb.get("supervisor_pid", -1)
            check("心跳文件记录子进程与守护 pid", child_pid > 0 and sup_pid > 0,
                  "child=%s supervisor=%s" % (child_pid, sup_pid))
            check("新入口不会误触发守护交接",
                  not os.path.exists(os.path.join(tmp, ".supervisor-handoff")),
                  "IFZ_SUPERVISOR_VERSION 已设为 2，交接只该在从 v1 升级时发生")

            kill(child_pid)
            h2 = wait_health(time.time() + 20)
            hb2 = heartbeat(tmp)
            check("杀掉大厅进程后自动拉起",
                  bool(h2) and hb2.get("child_pid") not in (-1, child_pid),
                  "new child=%s" % hb2.get("child_pid"))

            if sys.platform.startswith("linux"):
                again = subprocess.run([sys.executable, os.path.join(tmp, "deploy", "supervisor.py")],
                                       cwd=tmp, env=env, capture_output=True, timeout=30)
                check("单实例锁挡住第二个守护", again.returncode == 0,
                      (again.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
                      and (again.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-1]
                      or "no output")
            else:
                print("  skip 单实例锁（当前平台无 fcntl）")

            kill(sup_pid)
            # 旧大厅进程是孤儿子进程，仍能返回 /health，所以不能等 health，
            # 要等心跳里的守护 pid 真正换掉。
            deadline = time.time() + 30
            hb3 = heartbeat(tmp)
            while time.time() < deadline:
                hb3 = heartbeat(tmp)
                if hb3.get("supervisor_pid") not in (-1, sup_pid):
                    break
                time.sleep(0.5)
            h3 = health()
            ok3 = (bool(h3) and hb3.get("supervisor_pid") not in (-1, sup_pid)
                   and entry.poll() is None)
            check("杀掉守护进程后入口自动拉起", ok3,
                  "new supervisor=%s" % hb3.get("supervisor_pid"))
            if not ok3:
                for name, limit in (("boot.log", 15), ("supervisor.log", 15), ("lobby.log", 8)):
                    try:
                        with open(os.path.join(tmp, name), "rb") as f:
                            tail = f.read().decode("utf-8", "replace").splitlines()[-limit:]
                        print("    --- %s" % name)
                        for line in tail:
                            print("      " + line)
                    except Exception as e:
                        print("    --- %s unreadable (%s)" % (name, e))
                print("    --- 现场保留在 %s" % tmp)
                kept = True
        finally:
            log.close()
            kill(heartbeat(tmp).get("supervisor_pid", -1))
            kill(heartbeat(tmp).get("child_pid", -1))
            if entry.poll() is None:
                kill(entry.pid)
    finally:
        time.sleep(1)
        if FAIL:
            print("现场保留:", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("  - " + name)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
