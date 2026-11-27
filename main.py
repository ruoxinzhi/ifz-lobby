#!/usr/bin/env python3
"""面板入口。

很多托管面板只认项目根目录的 `main.py`，找不到就报「找不到 main.py」。
这个文件就是给它们的入口：解析端口，然后把活交给 `deploy/supervisor.py`
（守护 + 自更新），所以面板拉取代码后点「启动」即可。

端口来源，按优先级：
    IFZ_LOBBY_PORT  ->  平台注入的 PORT  ->  31531
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

port = (os.environ.get("IFZ_LOBBY_PORT")
        or os.environ.get("PORT")
        or "31531")
try:
    int(port)
except ValueError:
    port = "31531"

os.environ["IFZ_LOBBY_PORT"] = str(port)
os.environ.setdefault("IFZ_LOBBY_DATA", os.path.join(HERE, "rooms.json"))

sys.path.insert(0, HERE)
runpy.run_path(os.path.join(HERE, "deploy", "supervisor.py"), run_name="__main__")
