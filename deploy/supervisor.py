#!/usr/bin/env python3
"""IFZ 大厅守护进程（给没有 systemd / cron 的容器用）。

一个进程做六件事：

1. 单实例：flock 锁，面板手抖点两次 Start 也不会起两份抢同一个端口。
2. 清孤儿：上次异常退出留下的 `lobby_server.py` 会占着端口，启动时先收掉，
   否则新进程 bind 失败 -> 无限重启循环。
3. 守护：`lobby_server.py` 挂了就拉起（崩溃、OOM、手滑 kill 都算）。
4. 探活：定时 GET `/health`，**不是只看端口通不通**。进程卡死、线程全堵、
   socket 僵死这些「进程还在但不干活」的情况，只看端口是发现不了的。
5. 自更新：定时比对 GitHub 上的 sha256，变了才替换并重启；替换前 py_compile
   校验，新版本 15 秒内出不了 /health 就自动回滚。
6. 留痕：动作写 `supervisor.log`，另写 `.supervisor.heartbeat`，面板文件管理里
   就能看出守护还活着、子进程 pid 和内存多少。

用法（在 /opt/ifz-lobby 下）：

    python3 supervisor.py            # 前台
    setsid nohup python3 supervisor.py >/dev/null 2>&1 &    # 后台常驻

环境变量：
    IFZ_LOBBY_PORT        大厅端口，默认 31531
    IFZ_AUTOUPDATE_SECS   自更新间隔秒，默认 600；设 0 关闭
    IFZ_MAX_RSS_MB        子进程内存上限，超过就重启，默认 384
    IFZ_HEALTH_SECS       探活间隔，默认 15
"""
import hashlib
import json
import os
import re
import signal
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
HEARTBEAT = os.path.join(ROOT, ".supervisor.heartbeat")
LOCK = os.path.join(ROOT, ".supervisor.lock")

SUPERVISOR_VERSION = "2"


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


PORT = _env_int("IFZ_LOBBY_PORT", 0) or _env_int("PORT", 0) or 31531
CHECK_SECS = _env_int("IFZ_AUTOUPDATE_SECS", 600)
MAX_RSS_MB = _env_int("IFZ_MAX_RSS_MB", 384)
HEALTH_SECS = _env_int("IFZ_HEALTH_SECS", 15)

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
STARTUP_GRACE = 15.0       # 启动后多久内必须能返回 /health
HEALTH_FAILS = 3           # 连续几次探活失败才重启（防一次网络抖动误杀）

_stopping = False


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


def raise_nofile():
    try:
        import resource
        want = _env_int("IFZ_NOFILE", 65535)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = want if hard == resource.RLIM_INFINITY else min(want, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        return -1


def acquire_lock():
    """单实例。拿不到锁说明已有守护在跑，直接退出，别抢端口。

    非 POSIX 平台没有 fcntl，退化成「直接放行」——本地调试够用。
    """
    try:
        import fcntl
    except Exception:
        return True
    try:
        f = open(LOCK, "a+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        return True
    except Exception:
        return False


def reap_orphans():
    """收掉上次留下的 lobby_server.py，否则端口被占，新的永远起不来。

    只认命令行里带 lobby_server.py 的进程，绝不碰自己。
    """
    if not os.path.isdir("/proc"):
        return 0
    victims = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid <= 1 or pid == os.getpid():
            continue
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                raw = f.read()
        except Exception:
            continue
        if b"lobby_server.py" not in raw:
            continue
        victims.append(pid)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
            log("reaped orphan lobby_server pid=%d" % pid)
        except Exception:
            pass
    if victims:
        time.sleep(2)
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass          # 已经退出，正是我们想要的
    return len(victims)


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


def http_ok(timeout=2.5):
    """真正的探活：能返回 /health 且 ok=true 才算活。

    卡死的大厅进程照样能 accept（TCP 握手在内核队列里完成），所以
    `port_open` 只能证明「有人在听」，证明不了「它还在干活」。
    """
    url = "http://127.0.0.1:%d/health" % PORT
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ifz-supervisor"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        return bool(json.loads(body.decode("utf-8", "replace")).get("ok"))
    except Exception:
        return False


def rss_mb(pid):
    try:
        with open("/proc/%d/status" % pid, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


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
        if self.proc is not None:
            self.proc.poll()          # 回收已退出的旧进程，避免僵尸
        env = dict(os.environ)
        env["IFZ_LOBBY_PORT"] = str(PORT)
        env["IFZ_SUPERVISOR_VERSION"] = SUPERVISOR_VERSION
        env.setdefault("IFZ_LOBBY_DATA", os.path.join(ROOT, "rooms.json"))
        out = open(os.path.join(ROOT, "lobby.log"), "ab", buffering=0)
        self.proc = subprocess.Popen([sys.executable, TARGET],
                                     cwd=ROOT, env=env, stdout=out, stderr=out)
        self.started_at = time.time()
        log("started lobby_server pid=%d port=%d" % (self.proc.pid, PORT))
        touch_heartbeat(self)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self):
        return self.proc.pid if self.proc is not None else -1

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
        touch_heartbeat(self)


def apply_update(body, url):
    """写新文件并校验编译；失败则保持原状。"""
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


def restore_backup():
    try:
        with open(BACKUP, "rb") as f:
            data = f.read()
    except Exception as e:
        log("rollback failed: no backup (%s)" % e)
        return False
    with open(TARGET, "wb") as f:
        f.write(data)
    log("rolled back to %s" % version_of(data))
    return True


def deploy_and_verify(body, url, child):
    """更新 -> 重启 -> 必须在期限内返回 /health，否则回滚并再起一次。"""
    if not apply_update(body, url):
        return False
    child.stop()
    child.start()
    deadline = time.time() + STARTUP_GRACE
    while time.time() < deadline:
        time.sleep(1)
        if not child.alive():
            break
        if http_ok():
            log("update ok: %s is serving" % version_of(read_local()))
            return True
    log("new version failed health within %ss, rolling back" % int(STARTUP_GRACE))
    if restore_backup():
        child.stop()
        child.start()
    return False


def touch_heartbeat(child, extra=""):
    """面板文件管理里能直接看 mtime 在动，就知道守护还活着。"""
    try:
        data = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "supervisor_pid": os.getpid(),
            "supervisor_version": SUPERVISOR_VERSION,
            "child_pid": child.pid,
            "child_alive": child.alive(),
            "child_rss_mb": round(rss_mb(child.pid), 1) if child.pid > 0 else 0.0,
            "version": version_of(read_local()),
            "port": PORT,
            "note": extra,
        }
        tmp = HEARTBEAT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, HEARTBEAT)
    except Exception:
        pass


def _on_signal(signum, frame):
    global _stopping
    _stopping = True


def main():
    os.environ["IFZ_SUPERVISOR_VERSION"] = SUPERVISOR_VERSION
    log("supervisor v%s up | root=%s port=%d check=%ss health=%ss maxrss=%dMB nofile=%s"
        % (SUPERVISOR_VERSION, ROOT, PORT, CHECK_SECS, HEALTH_SECS, MAX_RSS_MB,
           raise_nofile()))

    if not acquire_lock():
        log("another supervisor already holds %s, exiting" % LOCK)
        return 0

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    killed = reap_orphans()
    if killed:
        log("orphan sweep: %d stale process(es) cleared" % killed)

    child = Child()
    child.start()
    now = time.time()
    next_health = now + STARTUP_GRACE
    next_check = now + 5          # 启动后先自查一次，之后按间隔
    next_beat = now
    fails = 0

    while not _stopping:
        time.sleep(1)

        if not child.alive():
            code = child.proc.returncode if child.proc else None
            log("lobby_server exited code=%s, restarting" % code)
            time.sleep(2)
            child.start()
            fails = 0
            now = time.time()
            next_health = now + STARTUP_GRACE
            next_check = now + 30
            continue

        now = time.time()

        if now >= next_beat:
            next_beat = now + 30
            touch_heartbeat(child)

        # 探活：连续失败才算死，避免一次抖动把好进程杀掉
        if now >= next_health:
            next_health = now + HEALTH_SECS
            if http_ok():
                fails = 0
            else:
                fails += 1
                log("health: no valid /health response (%d/%d)" % (fails, HEALTH_FAILS))
                if fails >= HEALTH_FAILS:
                    log("health: lobby_server unresponsive, restarting")
                    child.stop()
                    child.start()
                    fails = 0
                    next_health = now + STARTUP_GRACE
                    continue

        # 内存：容器就 1G，被 OOM killer 干掉会连带触发面板崩溃保护，
        # 不如自己先重启，代价只是一次一秒钟的重连。
        mb = rss_mb(child.pid)
        if mb > MAX_RSS_MB:
            log("child rss %.0fMB > %dMB, restarting to stay clear of OOM" % (mb, MAX_RSS_MB))
            child.stop()
            child.start()
            fails = 0
            next_health = now + STARTUP_GRACE
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
            deploy_and_verify(body, url, child)
            next_health = time.time() + STARTUP_GRACE

    log("supervisor stopping on signal")
    child.stop()
    touch_heartbeat(child, "stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("supervisor stopped by interrupt")
