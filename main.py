#!/usr/bin/env python3
"""面板入口（Pterodactyl / NobleHost 这类）+ 第一层保活。

启动命令只有一句 `python main.py`，而面板目录里可能一个仓库文件都没有。
所以这个文件兼任「引导器」：缺什么就从 GitHub 补什么，补完交给
`deploy/supervisor.py`（守护 + 自更新）。

也就是说：服务器上**只需要存在这一个文件**，其余自动就位。

保活分三层，这里是第一层：

1. **本文件**：永远不退。supervisor 崩了、被误杀、更新出错，都在 5 秒内
   原地重启它。面板因此看不到「进程退出」，也就不会触发
   "Aborting automatic restart, last crash occurred less than 60 seconds ago"
   那种崩溃保护冷却。
2. **supervisor**：盯住真正干活的大厅进程，端口不响应或内存超标就重启。
3. **大厅自身**：启动时自抬文件描述符上限。

端口按优先级取：
    IFZ_LOBBY_PORT -> PORT -> SERVER_PORT（面板分配的游戏端口） -> 31531
"""
import os
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "ruoxinzhi/ifz-lobby"
BRANCH = "main"
API = "https://api.github.com/repos/%s/contents/%%s?ref=%s" % (REPO, BRANCH)
RAW = "https://raw.githubusercontent.com/%s/%s/%%s" % (REPO, BRANCH)

FILES = ("lobby_server.py", "deploy/supervisor.py")

MIN_BACKOFF = 5           # 首次重启等待
MAX_BACKOFF = 60          # 连续崩溃时的退避上限
STABLE_SECS = 120         # 活过这么久就算「稳定」，退避归零

_stopping = False
_child = {"proc": None}


def log(msg):
    print("[ifz] " + msg, flush=True)


def raise_nofile():
    """每个中转客人占 2 个 socket。面板直接跑 main.py 时不会调 start.sh，
    所以在这里抬一次，后续进程全部继承。"""
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


def fetch(rel):
    """GitHub API 优先（即时，无 CDN 旧缓存），raw 兜底。"""
    headers = {"Accept": "application/vnd.github.raw", "User-Agent": "ifz-bootstrap"}
    for url in (API % rel, (RAW % rel) + "?v=%d" % int(time.time())):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
            if data and (b"def " in data or b"import " in data):
                return data
            log("skip %s: suspicious payload (%d bytes)" % (url.split("/")[2], len(data or b"")))
        except Exception as e:
            log("fetch %s failed: %s %s" % (url.split("/")[2], type(e).__name__, str(e)[:80]))
    return None


def ensure(rel):
    dst = os.path.join(HERE, rel.replace("/", os.sep))
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return True
    data = fetch(rel)
    if not data:
        log("MISSING %s and could not download it" % rel)
        return False
    parent = os.path.dirname(dst)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)
    log("bootstrapped %s (%d bytes)" % (rel, len(data)))
    return True


def _on_signal(signum, frame):
    """面板停服 -> 先礼后兵：让 supervisor 收尾，而不是留一堆孤儿。"""
    global _stopping
    _stopping = True
    proc = _child.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def sleep_short(seconds):
    """可被信号打断的 sleep，避免停服要等满退避时间。"""
    deadline = time.time() + seconds
    while not _stopping and time.time() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def watch_supervisor():
    """supervisor 崩溃就地重启，永不把退出码交给面板。"""
    backoff = MIN_BACKOFF
    path = os.path.join(HERE, "deploy", "supervisor.py")
    while not _stopping:
        started = time.time()
        try:
            env = os.environ.copy()
            env["IFZ_SUPERVISOR_VERSION"] = "2"     # 告诉大厅：守护是新的，别交接
            proc = subprocess.Popen([sys.executable, path], cwd=HERE, env=env)
            _child["proc"] = proc
        except Exception as e:
            log("cannot spawn supervisor: %s %s" % (type(e).__name__, str(e)[:120]))
            proc = None

        if proc is not None:
            while proc.poll() is None and not _stopping:
                time.sleep(1)
            rc = proc.returncode
            if _stopping:
                try:
                    proc.wait(timeout=10)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                log("supervisor stopped on signal")
                break
            lived = int(time.time() - started)
            if lived >= STABLE_SECS:
                backoff = MIN_BACKOFF
            log("supervisor exited rc=%s after %ss, restarting in %ss" % (rc, lived, backoff))

        _child["proc"] = None
        sleep_short(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


def main():
    port = (os.environ.get("IFZ_LOBBY_PORT")
            or os.environ.get("PORT")
            or os.environ.get("SERVER_PORT")
            or "31531")
    try:
        int(port)
    except ValueError:
        port = "31531"
    os.environ["IFZ_LOBBY_PORT"] = str(port)
    os.environ.setdefault("IFZ_LOBBY_DATA", os.path.join(HERE, "rooms.json"))

    nofile = raise_nofile()
    log("cwd=%s port=%s nofile=%s pid=%d" % (HERE, port, nofile, os.getpid()))

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    for rel in FILES:
        if not ensure(rel):
            log("cannot start: missing %s" % rel)
            return 1

    log("guard up: supervisor will be restarted if it ever exits")
    watch_supervisor()
    return 0


if __name__ == "__main__":
    sys.exit(main())
