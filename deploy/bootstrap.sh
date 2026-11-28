#!/bin/sh
# 从零部署 IFZ 联机大厅。
#
# 用途：新容器里什么都没有时，一条命令拉齐所有文件并启动。
# 用法：
#   sh bootstrap.sh            # 装到 /home/container
#   sh bootstrap.sh /opt/ifz-lobby
#
# 面板里可以直接把启动命令设成：
#   sh -c "curl -fsSL https://raw.githubusercontent.com/ruoxinzhi/ifz-lobby/main/deploy/bootstrap.sh | sh"
# 容器里没有 curl 时用：
#   python3 -c "import urllib.request;exec(urllib.request.urlopen('https://raw.githubusercontent.com/ruoxinzhi/ifz-lobby/main/deploy/bootstrap.sh').read().decode())"
#
# 端口：优先 IFZ_LOBBY_PORT，其次 PORT，再次面板注入的 SERVER_PORT，最后 31531。
# 管理令牌：已有 admin.token 就不动；否则用 IFZ_ADMIN_TOKEN，都没有就随机生成。

set -e

ROOT="${1:-/home/container}"
BASE="${IFZ_LOBBY_BASE:-https://raw.githubusercontent.com/ruoxinzhi/ifz-lobby/main}"

echo "[bootstrap] 目标目录 $ROOT"
cd "$ROOT"

get() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1"
    else
        python3 -c "import sys,urllib.request;open(sys.argv[2],'wb').write(urllib.request.urlopen(sys.argv[1],timeout=40).read())" "$1" "$2"
    fi
}

mkdir -p deploy
get "$BASE/lobby_server.py" lobby_server.py
get "$BASE/main.py" main.py
get "$BASE/deploy/supervisor.py" deploy/supervisor.py
chmod +x lobby_server.py main.py 2>/dev/null || true

if [ ! -s admin.token ]; then
    if [ -n "$IFZ_ADMIN_TOKEN" ]; then
        printf '%s' "$IFZ_ADMIN_TOKEN" > admin.token
    else
        python3 -c "import secrets;print(secrets.token_urlsafe(24),end='')" > admin.token
        echo "[bootstrap] 已生成管理令牌，见 $ROOT/admin.token"
    fi
    chmod 600 admin.token
else
    echo "[bootstrap] 沿用已有 admin.token"
fi

echo "[bootstrap] 版本 $(grep -m1 '^VERSION' lobby_server.py 2>/dev/null || echo '?')"
echo "[bootstrap] 启动"
exec python3 main.py
