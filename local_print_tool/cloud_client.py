"""
cloud_client.py — 云打印任务接收模块
通过 SocketIO 连接云端后端，接收小程序提交的打印任务，
下载文件，与主界面 GUI 通过 PySide6 Signal 通信。

架构:
  CloudClient (QObject)
    ├── SocketIO 长连接 (python-socketio)
    ├── HTTP 拉取 (requests) 作为补充
    ├── 后台文件下载
    └── PySide6 Signal 发射到 GUI 主线程
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from typing import Optional

import requests as http_requests

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# ---------- 常量 ----------

HEARTBEAT_INTERVAL = 30
RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 120


# ---------- CloudTask 数据结构 ----------

class CloudTask:
    """云端打印任务的数据封装"""
    __slots__ = (
        "task_id", "order_id", "file_name", "copies", "duplex",
        "page_range", "download_url", "created_at",
        "local_path", "download_progress", "status",
        "error_message",
    )

    def __init__(self, data: dict):
        options = data.get("options", {})
        self.task_id: int = data.get("task_id", data.get("id", 0))
        self.order_id: int | None = data.get("order_id")
        self.file_name: str = data.get("file_name", data.get("file", ""))
        self.copies: int = int(options.get("copies", data.get("copies", 1)))
        self.duplex: str = options.get("duplex", data.get("duplex", "on")) or "on"
        self.page_range: str = options.get("page_range", data.get("page_range", "")) or ""
        self.download_url: str = data.get("download_url", data.get("file_url", "")) or ""
        self.created_at: str = data.get("created_at", "")

        # 本地状态
        self.local_path: str = ""
        self.download_progress: int = 0
        self.status: str = "pending"  # pending | downloading | ready | accepted | rejected | error
        self.error_message: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "order_id": self.order_id,
            "file_name": self.file_name,
            "copies": self.copies,
            "duplex": self.duplex,
            "page_range": self.page_range,
            "download_url": self.download_url,
            "created_at": self.created_at,
            "local_path": self.local_path,
            "download_progress": self.download_progress,
            "status": self.status,
            "error_message": self.error_message,
        }


# ---------- CloudClient ----------

class CloudClient(QObject):
    """云端打印任务客户端。

    在后台线程中维护 SocketIO 长连接，接收实时推送的打印任务。
    所有 UI 更新通过 PySide6 Signal 发射到主线程。

    用法:
        client = CloudClient(api_url, ws_url, token, client_id)
        client.task_received.connect(on_task)
        client.connection_changed.connect(on_conn_change)
        client.start()
        ...
        client.accept_task(task_id)   # 接受任务并下载文件
        client.stop()
    """

    # ── 信号 ──
    task_received = Signal(object)        # CloudTask — 收到新任务
    task_updated = Signal(object)         # CloudTask — 任务状态更新（下载进度等）
    connection_changed = Signal(bool)     # True=已连接, False=已断开
    status_message = Signal(str)          # 日志消息

    def __init__(
        self,
        api_url: str = "",
        ws_url: str = "",
        token: str = "",
        client_id: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.api_url = api_url
        self.ws_url = ws_url
        self.token = token
        self.client_id = client_id

        # 内部状态
        self._sio: object | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._heartbeat_timer: threading.Timer | None = None
        self._reconnect_count = 0

        # 本地任务缓存
        self._pending_tasks: dict[int, CloudTask] = {}
        self._download_lock = threading.Lock()

    # ── 公共 API ──

    def start(self):
        """启动云客户端（后台线程）。"""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("CloudClient 已在运行")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cloud-client")
        self._thread.start()
        logger.info("CloudClient 已启动")

    def stop(self):
        """停止云客户端。"""
        self._stop_event.set()
        self._cancel_heartbeat()
        if self._sio:
            try:
                self._sio.disconnect()
            except Exception:
                pass
            self._sio = None
        logger.info("CloudClient 已停止")

    def is_connected(self) -> bool:
        return self._connected

    def get_pending_tasks(self) -> list[CloudTask]:
        """返回当前待处理任务列表。"""
        return list(self._pending_tasks.values())

    def accept_task(self, task_id: int):
        """接受一个任务：开始下载文件。"""
        task = self._pending_tasks.get(task_id)
        if not task:
            return
        if task.status not in ("pending", "error"):
            return
        task.status = "downloading"
        task.download_progress = 0
        task.error_message = ""
        self.task_updated.emit(task)
        t = threading.Thread(target=self._download_file, args=(task,), daemon=True)
        t.start()

    def reject_task(self, task_id: int):
        """拒绝一个任务：从列表移除，不上报。"""
        task = self._pending_tasks.pop(task_id, None)
        if task:
            task.status = "rejected"
            self.task_updated.emit(task)

    def accept_and_add_to_local(self, task_id: int) -> CloudTask | None:
        """标记任务为已接受（下载完成后调用），返回任务供 GUI 添加到本地列表。"""
        task = self._pending_tasks.pop(task_id, None)
        if task:
            task.status = "accepted"
        return task

    def report_success(self, task_id: int):
        """上报打印成功到云端。"""
        if self._sio and self._connected:
            try:
                self._sio.emit("print_success", {"task_id": task_id})
                self.status_message.emit(f"☁ 任务 #{task_id} 上报: 打印成功")
            except Exception as e:
                logger.warning(f"上报 print_success 失败: {e}")

    def report_fail(self, task_id: int, error: str):
        """上报打印失败到云端。"""
        if self._sio and self._connected:
            try:
                self._sio.emit("print_fail", {"task_id": task_id, "error": error})
                self.status_message.emit(f"☁ 任务 #{task_id} 上报: 打印失败 — {error}")
            except Exception as e:
                logger.warning(f"上报 print_fail 失败: {e}")

    def pull_pending(self):
        """HTTP 拉取云端排队任务（作为 SocketIO 推送的补充）。"""
        if not self.api_url or not self.token:
            return
        try:
            resp = http_requests.get(
                f"{self.api_url}/api/pull_queued_orders",
                params={"token": self.token, "client_id": self.client_id},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                if data.get("success") and data.get("orders"):
                    for item in data["orders"]:
                        task = CloudTask(item)
                        if task.task_id not in self._pending_tasks:
                            self._pending_tasks[task.task_id] = task
                            self.task_received.emit(task)
                            self.status_message.emit(
                                f"☁ HTTP 拉取到任务 #{task.task_id}: {task.file_name}"
                            )
        except Exception as e:
            logger.debug(f"HTTP 拉取排队任务失败: {e}")

    # ── 内部实现 ──

    def _run_loop(self):
        """后台主循环：连接 → 维持 → 重连。"""
        while not self._stop_event.is_set():
            try:
                self._connect_and_wait()
            except Exception as e:
                logger.warning(f"CloudClient 连接异常: {e}")
            if not self._stop_event.is_set():
                delay = min(RECONNECT_BASE_DELAY * (2 ** self._reconnect_count), RECONNECT_MAX_DELAY)
                self._reconnect_count += 1
                self.status_message.emit(f"☁ {delay}s 后重连...")
                self._stop_event.wait(delay)

    def _connect_and_wait(self):
        """建立 SocketIO 连接并阻塞等待，直到断开或停止。"""
        import socketio as socketio_lib

        self._sio = socketio_lib.Client(
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )

        connect_event = threading.Event()

        @self._sio.on("connect")
        def _on_connect():
            self._connected = True
            self._reconnect_count = 0
            self.connection_changed.emit(True)
            self.status_message.emit("☁ 已连接到云端服务器")
            self._start_heartbeat()
            # 连接后拉取排队任务
            self.pull_pending()
            connect_event.set()

        @self._sio.on("disconnect")
        def _on_disconnect():
            self._connected = False
            self.connection_changed.emit(False)
            self.status_message.emit("☁ 已断开云端连接")
            self._cancel_heartbeat()
            connect_event.set()

        @self._sio.on("print_task")
        def _on_print_task(data):
            task = CloudTask(data)
            if task.task_id not in self._pending_tasks:
                self._pending_tasks[task.task_id] = task
                self.task_received.emit(task)
                self.status_message.emit(
                    f"☁ 收到云任务 #{task.task_id}: {task.file_name} "
                    f"({task.copies}份, {'双面' if task.duplex == 'on' else '单面'})"
                )
                # 自动开始下载
                self.accept_task(task.task_id)

        @self._sio.on("pong")
        def _on_pong():
            pass

        @self._sio.on("auth_fail")
        def _on_auth_fail(data):
            msg = data.get("message", "未知原因") if isinstance(data, dict) else str(data)
            self.status_message.emit(f"☁ 认证失败: {msg}")
            self._stop_event.set()

        # 连接
        connect_url = f"{self.ws_url}?token={self.token}&client_id={self.client_id}"
        self.status_message.emit(f"☁ 正在连接 {self.ws_url} ...")
        try:
            self._sio.connect(connect_url, wait_timeout=10)
        except Exception as e:
            self.status_message.emit(f"☁ 连接失败: {e}")
            return

        # 阻塞等待断开
        while self._connected and not self._stop_event.is_set():
            connect_event.wait(1.0)
            connect_event.clear()

    # ── 心跳 ──

    def _start_heartbeat(self):
        self._cancel_heartbeat()
        self._schedule_heartbeat()

    def _schedule_heartbeat(self):
        if self._stop_event.is_set():
            return
        self._heartbeat_timer = threading.Timer(HEARTBEAT_INTERVAL, self._send_heartbeat)
        self._heartbeat_timer.daemon = True
        self._heartbeat_timer.start()

    def _send_heartbeat(self):
        if self._sio and self._connected:
            try:
                self._sio.emit("ping")
            except Exception:
                pass
        self._schedule_heartbeat()

    def _cancel_heartbeat(self):
        if self._heartbeat_timer:
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None

    # ── 文件下载 ──

    def _download_file(self, task: CloudTask):
        """后台下载文件。成功则 task.local_path 有值，status='ready'。"""
        task_id = task.task_id
        url = task.download_url

        if not url:
            task.status = "error"
            task.error_message = "缺少下载链接"
            self.task_updated.emit(task)
            return

        try:
            self.status_message.emit(f"☁ 开始下载 #{task_id}: {task.file_name}")
            resp = http_requests.get(url, timeout=120, stream=True)
            resp.raise_for_status()

            # 从 Content-Disposition 提取原始文件名
            original_name = task.file_name
            cd = resp.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                m = re.search(r'filename[*]?=(?:UTF-8\'\')?(?:"([^"]+)"|([^;]+))', cd, re.I)
                if m:
                    original_name = m.group(1) or m.group(2)

            if not original_name:
                original_name = f"cloud_task_{task_id}.dat"

            dest = os.path.join(tempfile.gettempdir(), f"hn_cloud_{task_id}_{original_name}")

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        progress = int(downloaded / total * 100)
                        if progress != task.download_progress:
                            task.download_progress = progress
                            self.task_updated.emit(task)

            task.local_path = dest
            task.status = "ready"
            task.download_progress = 100
            self.task_updated.emit(task)
            self.status_message.emit(
                f"☁ 下载完成 #{task_id}: {original_name} ({os.path.getsize(dest)} bytes)"
            )

        except Exception as e:
            task.status = "error"
            task.error_message = str(e)
            self.task_updated.emit(task)
            self.status_message.emit(f"☁ 下载失败 #{task_id}: {e}")
