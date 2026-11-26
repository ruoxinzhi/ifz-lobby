#!/usr/bin/env bash
# 首次安装：sudo bash deploy/install.sh [端口]
set -euo pipefail

PORT="${1:-31531}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST="/opt/ifz-lobby"

[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }
command -v python3 >/dev/null || { echo "缺 python3，先装：apt-get install -y python3"; exit 1; }

mkdir -p "$DST"
cp -f "$SRC/lobby_server.py" "$DST/lobby_server.py"
chmod 0755 "$DST/lobby_server.py"

sed "s/IFZ_LOBBY_PORT=[0-9]*/IFZ_LOBBY_PORT=$PORT/" \
    "$SRC/deploy/ifz-lobby.service" > /etc/systemd/system/ifz-lobby.service
sed -i "s#WorkingDirectory=.*#WorkingDirectory=$DST#; s#ExecStart=.*#ExecStart=/usr/bin/python3 $DST/lobby_server.py#; s#IFZ_LOBBY_DATA=.*#IFZ_LOBBY_DATA=$DST/rooms.json#" \
    /etc/systemd/system/ifz-lobby.service

systemctl daemon-reload
systemctl enable ifz-lobby >/dev/null 2>&1 || true
systemctl restart ifz-lobby
sleep 2

systemctl is-active --quiet ifz-lobby || {
    echo "服务没起来，最近日志："; journalctl -u ifz-lobby -n 30 --no-pager; exit 1; }

echo "已安装：端口 $PORT，数据 $DST/rooms.json"
curl -sS -m 5 "http://127.0.0.1:$PORT/health" || { echo "健康检查失败"; exit 1; }
echo
