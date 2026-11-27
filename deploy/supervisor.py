#!/usr/bin/env python3
"""IFZ 大厅守护进程（给没有 systemd / cron 的容器用）。

一个进程做三件事：

1. 守护：`lobby_server.py` 挂了就拉起来（崩溃、OOM、手滑 kill 都算）。
2. 自更新：定时比对 GitHub 上 `lobby_server.py` 的 sha256，不一样才替换并重启。
   替换前先 py_compile 检查、留备份；新版本起不来就自动回滚。
3. 留痕：所有动作写 `supervisor.log`，`/health` 里也有版本号可对照。

用法（在 /opt/ifz-lobby 下）：

    python3 supervisor.py            # 前台
    setsid nohup python3 supervisor.py >/dev/null 2>&1 &    # 后台常驻

环境变量：
    IFZ_LOBBY_PORT        大厅端口，默认 31531
    IFZ_AUTOUPDATE_SECS   检查间隔秒，默认 600；设 0 关闭自更新
"""
import hashlib
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TARGET = os.path.join(ROOT, "lobby_server.py")
BACKUP = TARGET + ".bak"
LOG = os.path.join(ROOT, "supervisor.log")

PORT = int(os.environ.get("IFZ_LOBBY_PORT", "31531"))
CHECK_SECS = int(os.environ.get("IFZ_AUTOUPDATE_SECS", "600"))

REPO = "ruoxinzhi/ifz-lobby"
# raw.githubusercontent 和 jsDelivr 都走 CDN，查询参数绕不过去（实测 ?t= 无效，
# 会读到几分钟前的旧副本）。GitHub Contents API 才是即时的，所以它排第一；
# 匿名额度 60 次/小时，按 10 分钟一轮是 6 次，够用。剩下两个只在 API 失败时兜底。
API_URL = ("https://api.github.com/repos/%s/contents/lobby_server.py?ref=main" % REPO,
           {"Accept": "application/vnd.github.raw", "User-Agent": "ifz-supervisor"})
CDN_URLS = (
    "https://cdn.jsdelivr.net/gh/%s@main/lobby_server.py?v=%%d" % REPO,
    "https://raw.githubusercontent.com/%s/main/lobby_server.py?v=%%d" % REPO,
)

VERSION_RE = re.compile(rb'^VERSION = "([^"]+)"', re.M)
STARTUP_GRACE = 15.0      # 启动后多久内必须能连上端口
HEALTH_EVERY = 20.0


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 5 * 1024 * 1024:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def version_of(data):
    m = VERSION_RE.search(data or b"")
    return m.group(1).decode() if m else "?"


def digest(data):
    return hashlib.sha256(data or b"").hexdigest()


def read_local():
    try:
        with open(TARGET, "rb") as f:
            return f.read()
    except Exception:
        return b""


def fetch_remote():
    stamp = int(time.time())
    candidates = [(API_URL[0], API_URL[1])]
    candidates += [(u % stamp, {}) for u in CDN_URLS]
    for url, headers in candidates:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
            if version_of(body) == "?":
                log("skip %s: no VERSION marker" % url.split("/")[2])
                continue
            return body, url
        except Exception as e:
            log("fetch failed %s: %s %s" % (url.split("/")[2], type(e).__name__, str(e)[:80]))
    return None, None


def port_open(timeout=1.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def compile_ok(path):
    try:
        p = subprocess.run([sys.executable, "-m", "py_compile", path],
                           capture_output=True, timeout=30)
        return p.returncode == 0, (p.stderr or b"").decode("utf-8", "replace")[:300]
    except Exception as e:
        return False, str(e)


class Child(object):
    def __init__(self):
        self.proc = None
        self.started_at = 0.0

    def start(self):
        env = dict(os.environ)
        env["IFZ_LOBBY_PORT"] = str(PORT)
        env.setdefault("IFZ_LOBBY_DATA", os.path.join(ROOT, "rooms.json"))
        out = open(os.path.join(ROOT, "lobby.log"), "ab", buffering=0)
        self.proc = subprocess.Popen([sys.executable, TARGET],
                                     cwd=ROOT, env=env, stdout=out, stderr=out)
        self.started_at = time.time()
        log("started lobby_server pid=%d port=%d" % (self.proc.pid, PORT))

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if not self.alive():
            return
        pid = self.proc.pid
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        log("stopped lobby_server pid=%d" % pid)


def apply_update(body, url):
    """写新文件 -> 重启 -> 15 秒内端口必须通，否则回滚。"""
    old = read_local()
    if old:
        try:
            with open(BACKUP, "wb") as f:
                f.write(old)
        except Exception as e:
            log("backup failed: %s" % e)
            return False

    tmp = TARGET + ".new"
    with open(tmp, "wb") as f:
        f.write(body)
    ok, err = compile_ok(tmp)
    if not ok:
        log("new file does not compile, keep current: %s" % err)
        os.remove(tmp)
        return False
    os.replace(tmp, TARGET)
    log("updated %s -> %s via %s" % (version_of(old), version_of(body), url.split("/")[2]))
    return True


def main():
    log("supervisor up | root=%s port=%d check=%ss" % (ROOT, PORT, CHECK_SECS))
    child = Child()
    child.start()
    next_check = time.time() + 5        # 启动后先自查一次，之后按间隔
    last_health = time.time()
    pending_rollback = False

    while True:
        time.sleep(1)

        if not child.alive():
            code = child.proc.returncode if child.proc else None
            log("lobby_server exited code=%s, restarting" % code)
            time.sleep(2)
            child.start()
            pending_rollback = False
            next_check = time.time() + 30
            continue

        now = time.time()

        # 刚启动：给足时间，端口仍不通就当成起不来
        if now - child.started_at > STARTUP_GRACE and now - last_health > HEALTH_EVERY:
            last_health = now
            if not port_open():
                log("health: port %d not listening, restarting" % PORT)
                child.stop()
                child.start()
                if pending_rollback:
                    log("new version never came up, rolling back")
                    restore_backup()
                    pending_rollback = False
                continue

        if CHECK_SECS > 0 and now >= next_check:
            next_check = now + CHECK_SECS
            body, url = fetch_remote()
            if body is None:
                continue
            local = read_local()
            if digest(body) == digest(local):
                log("check: up to date (%s)" % version_of(local))
                continue
            log("check: remote %s != local %s, updating"
                % (version_of(body), version_of(local)))
            if apply_update(body, url):
                child.stop()
                child.start()
                pending_rollback = True


def restore_backup():
    try:
        with open(BACKUP, "rb") as f:
            data = f.read()
    except Exception as e:
        log("rollback failed: no backup (%s)" % e)
        return
    with open(TARGET, "wb") as f:
        f.write(data)
    log("rolled back to %s" % version_of(data))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("supervisor stopped by signal")
