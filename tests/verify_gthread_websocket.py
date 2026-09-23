# -*- coding: utf-8 -*-
"""验收脚本 · gunicorn gthread worker 下的 WebSocket 可用性

gunicorn gthread worker + Flask-SocketIO(async_mode="threading") + simple-websocket 时，
WebSocket 连接、往返事件、**服务端后台线程主动推送**是否都正常。

打印系统依赖的三件事：① 客户端以 websocket 连上；② 客户端事件（ping/回报）能到服务端；
③ 服务端在后台线程里 socketio.emit(..., to=sid) 能把 print_task 推到客户端。
③ 尤其关键——simple-websocket 需要从 WSGI environ 拿原始 socket（'gunicorn.socket'）。

在装了后端依赖的环境里跑（本机 127.0.0.1:5099 起一个独立小应用，不碰生产进程/DB；
客户端侧需要 websocket-client，后端 venv 里没有的话可 --target 装到临时目录后加 PYTHONPATH）：
    /home/printer-backend/venv/bin/python tests/verify_gthread_websocket.py
"""
import os
import subprocess
import sys
import threading
import time

APP_SRC = r'''
import threading
from flask import Flask, request
from flask_socketio import SocketIO

app = Flask(__name__)
app.config["SECRET_KEY"] = "check"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
_sid = {}


@socketio.on("connect")
def _on_connect(auth=None):
    _sid["v"] = request.sid
    socketio.emit("printer_state", {"is_active": True}, to=request.sid)


@socketio.on("ping")
def _on_ping():
    socketio.emit("pong", to=request.sid)


@app.route("/push")
def _push():
    sid = _sid.get("v")
    threading.Thread(
        target=lambda: socketio.emit("print_task", {"task_id": 4242}, to=sid),
        daemon=True).start()
    return "ok"


if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=5099)
'''

HERE = os.path.dirname(os.path.abspath(__file__))
open(os.path.join(HERE, "ws_check_app.py"), "w").write(APP_SRC)

port = 5099
g = subprocess.Popen(
    [sys.executable, "-m", "gunicorn", "-k", "gthread", "--threads", "4",
     "-w", "1", "-b", f"127.0.0.1:{port}", "--log-level", "warning", "ws_check_app:app"],
    cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(3.0)

import requests                     # noqa: E402
import socketio as socketio_lib     # noqa: E402

got = {}
c = socketio_lib.Client(reconnection=False, request_timeout=20)


@c.on("printer_state")
def _ps(data):
    got["printer_state"] = data


@c.on("print_task")
def _pt(data):
    got["print_task"] = data


@c.on("pong")
def _po():
    got["pong"] = True


res = {}
try:
    c.connect(f"http://127.0.0.1:{port}", transports=["websocket"], wait_timeout=20)
    res["websocket 连接建立"] = bool(c.connected)
    res["传输方式 = websocket"] = c.transport() == "websocket"
    res["connect 时的服务端推送（printer_state）"] = got.get("printer_state", {}).get("is_active") is True

    c.emit("ping")
    for _ in range(50):
        if got.get("pong"):
            break
        time.sleep(0.1)
    res["客户端→服务端→客户端往返（ping/pong）"] = bool(got.get("pong"))

    r = requests.get(f"http://127.0.0.1:{port}/push", timeout=5)
    for _ in range(50):
        if got.get("print_task"):
            break
        time.sleep(0.1)
    res["后台线程主动推送（print_task, 打印任务同款路径）"] = (
        r.ok and got.get("print_task", {}).get("task_id") == 4242)
finally:
    try:
        c.disconnect()
    except Exception:
        pass
    g.terminate()
    try:
        g.wait(timeout=5)
    except Exception:
        g.kill()

for k, v in res.items():
    print(f"[{'PASS' if v else 'FAIL'}] {k}")
sys.exit(0 if res and all(res.values()) else 1)
