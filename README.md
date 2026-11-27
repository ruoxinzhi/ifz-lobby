# IFZ 大厅 / 中转服务

感染免区（Infection Free Zone）联机用的**房间目录 + TCP 中转**服务端。
纯 Python 3 标准库，没有第三方依赖，单文件 520 行。

一台服务器跑这一个进程就够：它同时提供

- 房间目录：玩家在主菜单搜房间，房主注册 / 心跳 / 注销
- TCP 中转：`CONNECT` 隧道，给连不上房主公网 IP 的客人转发游戏流量

**它不存游戏存档，也不解析游戏协议**，只转发字节。

---

## 一、首次部署（每台服务器做一次）

```bash
sudo apt-get update && sudo apt-get install -y git python3
sudo git clone <本仓库地址> /opt/ifz-lobby
sudo bash /opt/ifz-lobby/deploy/install.sh 31531
```

`31531` 换成这台机器要用的端口。装完会立刻起服务并自检，输出：

```
{"ok": true, "rooms": 0, "ttl": 90, "max_rooms": 200}
```

看到 `"ok": true` 就成了。

> 端口要和客户端 `modpack.json` 里写的对得上，否则玩家搜不到房。

---

## 二、以后更新（改完代码就这一条）

```bash
sudo bash /opt/ifz-lobby/deploy/update.sh
```

它做四件事：拉最新代码 → 重启服务 → 探一次 `/health` → **健康检查失败就自动回滚到上一个提交并重启**。
所以更新坏了不会把线上打死。

### 没有命令行、只有网页控制面板怎么办

面板通常能给到下面两种能力之一，任选其一，都不用敲命令：

**A. 面板有「Git / 远程仓库 / 代码部署」**

| 填什么 | 值 |
|---|---|
| 仓库地址 | `https://github.com/ruoxinzhi/ifz-lobby.git` |
| 分支 | `main` |
| 部署目录 | 你现在放 `lobby_server.py` 的那个目录（如 `/opt/ifz-lobby`） |

点「拉取 / 部署」，然后到面板的「进程 / 服务」里把 `ifz-lobby` 重启一次。

**B. 面板只有「文件管理」**

1. 浏览器打开 `https://raw.githubusercontent.com/ruoxinzhi/ifz-lobby/main/lobby_server.py`
2. `Ctrl+A` 全选、`Ctrl+C` 复制
3. 面板文件管理里打开服务器上的 `lobby_server.py`，全选、粘贴、保存
4. 回面板重启该进程

如果面板有「远程下载 / 从 URL 下载」，直接用它下载上面那个地址覆盖文件，更省事。

**验证是否真的更新了**：浏览器打开 `http://你的域名或IP:端口/health`，
看到 `"version": "2026-11-27.1"` 就是新版。以后每次更新我都会把这个号加一，
看不到新号就说明文件没覆盖成功或进程没重启。

---

## 三、多台服务器

每台都按上面装一次，端口可以不同。客户端的 `Multiplayer.LobbyUrl`
支持逗号分隔多台，房主会往每台都登记、并在每台预留中转座位：

```json
"LobbyUrl": "http://服务器A:31531,http://服务器B:25927,http://服务器C:26055"
```

一台挂了不影响其它台，客户端会自己换。新增一台后，**记得把地址同步到模组默认值**
（`NetLobby.DefaultUrl`、`build_presets.py`、四份配置），否则老玩家配置里没有它。

---

## 四、常用排错

| 现象 | 先看什么 |
|---|---|
| 玩家搜不到房 | `curl 127.0.0.1:端口/health` 是否 200；端口是否放行 |
| 能搜到但进不去 | 中转端口是否被防火墙拦；`journalctl -u ifz-lobby -n 50` |
| 服务起不来 | `journalctl -u ifz-lobby -n 50`；端口被占 `ss -lntp \| grep 端口` |
| 房间不消失 | TTL 默认 90 秒，看 `/health` 的 `ttl` |

```bash
systemctl status ifz-lobby
journalctl -u ifz-lobby -f
curl -s http://127.0.0.1:31531/health
```

---

## 六、没有 systemd / cron 的容器

有些云主机只是个容器：`systemctl`、`crontab`、`git` 全没有，只有 `python3`。
这种用 `deploy/supervisor.py`，它自己就是守护进程 + 更新器：

```sh
cd /opt/ifz-lobby
sh deploy/start.sh          # 后台常驻（幂等，重复执行不会起两份）
```

它做三件事：

1. `lobby_server.py` 挂了自动拉起（崩溃、OOM、误 kill）
2. 每 10 分钟比对 GitHub 上文件的 sha256，变了才替换并重启
3. 新版本 15 秒内起不来 → 自动回滚到上一份

日志在 `supervisor.log` 和 `lobby.log`，浏览器看 `http://IP:端口/health`
的 `version` 就知道当前跑的是哪版。

**开机自启**：容器没有 systemd/cron，只能靠面板。在面板的「启动命令 / 开机任务」
里填这一句即可（只需设置一次）：

```sh
sh /opt/ifz-lobby/deploy/start.sh
```

关闭自更新：`IFZ_AUTOUPDATE_SECS=0`。

---

## 五、接口（排错用）

```
GET    /health                         服务状态
GET    /api/rooms?q=关键词              搜房间
POST   /api/room                       房主注册，返回 code + token
POST   /api/room/<code>/heartbeat      保活
DELETE /api/room/<code>?token=...      注销
CONNECT host:port                      中转隧道
```

环境变量：`IFZ_LOBBY_PORT`（默认 8080）、`IFZ_LOBBY_DATA`（rooms.json 路径）、
`IFZ_LOBBY_TTL`（默认 90 秒）、`IFZ_LOBBY_MAX`（默认 200 间）、
`IFZ_RELAY_SEATS`（每台预留中转座位，默认 7）。
