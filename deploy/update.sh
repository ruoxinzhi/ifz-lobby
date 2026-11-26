#!/usr/bin/env bash
# 日常更新：sudo bash deploy/update.sh
# 拉代码 -> 重启 -> 健康检查；失败自动回滚到更新前的提交。
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="$(systemctl show -p Environment ifz-lobby 2>/dev/null | grep -o 'IFZ_LOBBY_PORT=[0-9]*' | cut -d= -f2)"
PORT="${PORT:-31531}"

[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }
cd "$DIR" || exit 1

OLD="$(git rev-parse HEAD)"
echo "更新前提交：$OLD"

if ! git pull --ff-only; then
    echo "git pull 失败（本地有改动或网络不通），未重启服务"
    exit 1
fi

NEW="$(git rev-parse HEAD)"
cp -f "$DIR/lobby_server.py" /opt/ifz-lobby/lobby_server.py
systemctl restart ifz-lobby
sleep 3

if curl -sS -m 5 "http://127.0.0.1:$PORT/health" | grep -q '"ok": *true'; then
    echo "更新成功：$OLD -> $NEW"
    exit 0
fi

echo "健康检查失败，回滚到 $OLD"
git reset --hard "$OLD" >/dev/null
cp -f "$DIR/lobby_server.py" /opt/ifz-lobby/lobby_server.py
systemctl restart ifz-lobby
sleep 2
curl -sS -m 5 "http://127.0.0.1:$PORT/health" || true
echo
echo "已回滚。日志："; journalctl -u ifz-lobby -n 30 --no-pager
exit 1
