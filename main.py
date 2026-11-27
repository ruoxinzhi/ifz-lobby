#!/usr/bin/env python3
"""面板入口（Pterodactyl / NobleHost 这类）。

启动命令只有一句 `python main.py`，而面板目录里可能一个仓库文件都没有。
所以这个文件兼任「引导器」：缺什么就从 GitHub 补什么，补完交给
`deploy/supervisor.py`（守护 + 自更新）。

也就是说：服务器上**只需要存在这一个文件**，其余自动就位。

端口按优先级取：
    IFZ_LOBBY_PORT -> PORT -> SERVER_PORT（面板分配的游戏端口） -> 31531
"""
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "ruoxinzhi/ifz-lobby"
BRANCH = "main"
API = "https://api.github.com/repos/%s/contents/%%s?ref=%s" % (REPO, BRANCH)
RAW = "https://raw.githubusercontent.com/%s/%s/%%s" % (REPO, BRANCH)

FILES = ("lobby_server.py", "deploy/supervisor.py")


def log(msg):
    print("[ifz] " + msg, flush=True)


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
    log("cwd=%s port=%s" % (HERE, port))

    for rel in FILES:
        if not ensure(rel):
            log("cannot start: missing %s" % rel)
            return 1

    sys.path.insert(0, HERE)
    import runpy
    runpy.run_path(os.path.join(HERE, "deploy", "supervisor.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
