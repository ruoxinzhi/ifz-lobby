#!/bin/sh
# 无 systemd / cron 的容器：后台常驻启动（幂等，重复执行不会起两份）
cd "$(dirname "$0")/.." || exit 1

if ps -ef 2>/dev/null | grep -v grep | grep -q "deploy/supervisor.py"; then
    echo "supervisor already running"
    exit 0
fi

IFZ_LOBBY_PORT="${IFZ_LOBBY_PORT:-31531}"
export IFZ_LOBBY_PORT
setsid nohup python3 deploy/supervisor.py >/dev/null 2>&1 &
sleep 2
echo "supervisor started on port $IFZ_LOBBY_PORT"
