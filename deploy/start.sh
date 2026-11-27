#!/bin/sh
# 无 systemd / cron 的容器：后台常驻启动（幂等，重复执行不会起两份）
cd "$(dirname "$0")/.." || exit 1

if ps -ef 2>/dev/null | grep -v grep | grep -q "deploy/supervisor.py"; then
    echo "supervisor already running"
    exit 0
fi

IFZ_LOBBY_PORT="${IFZ_LOBBY_PORT:-31531}"
export IFZ_LOBBY_PORT
# 每个中转客人 2 个 socket；默认 1024 撑不到 500 人。
ulimit -n 65535 2>/dev/null || echo "warn: cannot raise nofile, keep $(ulimit -n 2>/dev/null)"
export IFZ_LOBBY_MAX="${IFZ_LOBBY_MAX:-120}"
export IFZ_LOBBY_MAX_PER_IP="${IFZ_LOBBY_MAX_PER_IP:-8}"
setsid nohup python3 deploy/supervisor.py >/dev/null 2>&1 &
sleep 2
echo "supervisor started on port $IFZ_LOBBY_PORT"
