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

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import requests as http_requests

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# ---------- 常量 ----------

HEARTBEAT_INTERVAL = 30
RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 120


def get_cached_pdf_path(source_md5: str) -> str | None:
    """模块级函数：检查 PDF 缓存（供 PrintWorker 使用）。
    返回缓存 PDF 路径，无缓存返回 None。"""
    if not source_md5:
        return None
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_cache")
    pdf_path = os.path.join(cache_dir, f"{source_md5}.pdf")
    if os.path.isfile(pdf_path):
        return pdf_path
    return None


# ---------- CloudTask 数据结构 ----------

class CloudTask:
    """云端打印任务的数据封装"""
    __slots__ = (
        "task_id", "order_id", "order_number", "file_name", "copies", "duplex",
        "page_range", "download_url", "created_at",
        "local_path", "download_progress", "status",
        "error_message", "source_md5",
        "delivery_enabled", "delivery_location", "urgency", "cover_page", "cover_page_price",
        "auto_print",
    )

    def __init__(self, data: dict):
        options = data.get("options", {})
        self.task_id: int = data.get("task_id", data.get("id", 0))
        self.order_id: int | None = data.get("order_id")
        self.order_number: str = data.get("order_number", "")
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
        self.source_md5: str = data.get("source_md5", "") or ""  # 后端传来的文件 MD5，用于 PDF 缓存查找
        # 附加服务（来自前端订单配置，传递给本地标签页）
        self.delivery_enabled: bool = bool(data.get("delivery_enabled", False))
        self.delivery_location: str = data.get("delivery_location", "") or ""
        self.urgency: str = data.get("urgency", "低") or "低"
        self.cover_page: bool = bool(data.get("cover_page", False))
        self.cover_page_price: float = float(data.get("cover_page_price", 0.15) or 0.15)
        self.auto_print: bool = bool(data.get("auto_print", False))  # 无障碍打印

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "order_id": self.order_id,
            "order_number": self.order_number,
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
    order_canceled = Signal(int, list)    # int=order_id, list=task_ids — 订单被用户取消

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

    def abandon_order_to_server(self, order_id: int):
        """放弃已接受的订单：调用后端 API。"""
        if not self.api_url or not self.token:
            self._queue_status_sync(order_id, "abandoned")
            return
        self._try_status_sync(order_id, "abandoned")

    def accept_order_to_server(self, order_id: int):
        """确认接受订单：调用后端 API。失败时加入同步队列。"""
        if not self.api_url or not self.token:
            self._queue_status_sync(order_id, "accepted")
            return
        self._try_status_sync(order_id, "accepted")
        if not self.api_url or not self.token:
            return
        try:
            resp = http_requests.post(
                f"{self.api_url}/api/accept_order",
                params={"token": self.token},
                json={"order_id": order_id},
                timeout=10,
            )
            if resp.ok:
                self.status_message.emit(f"☁ 订单 #{order_id} 已确认接受")
        except Exception as e:
            self.status_message.emit(f"☁ 确认订单 #{order_id} 异常: {e}")

    def reject_order_to_server(self, order_id: int):
        """打回订单：调用后端 API，将订单状态设为 rejected。"""
        if not self.api_url or not self.token:
            return
        try:
            resp = http_requests.post(
                f"{self.api_url}/api/reject_order",
                params={"token": self.token},
                json={"order_id": order_id},
                timeout=10,
            )
            if resp.ok:
                self.status_message.emit(f"☁ 订单 #{order_id} 已打回")
            else:
                self.status_message.emit(f"☁ 打回订单 #{order_id} 失败: {resp.text}")
        except Exception as e:
            self.status_message.emit(f"☁ 打回订单 #{order_id} 异常: {e}")

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

    # ── 离线状态同步 ──

    def _status_queue_path(self) -> str:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_cache")
        return os.path.join(d, "status_queue.json")

    def _queue_status_sync(self, order_id: int, status: str):
        """离线时将状态变更暂存到本地队列。"""
        import json as _json
        path = self._status_queue_path()
        queue = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    queue = _json.load(f)
            except Exception:
                queue = []
        queue.append({"order_id": order_id, "status": status, "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(queue, f, ensure_ascii=False, indent=2)
        self.status_message.emit(f"☁ 状态同步已暂存: 订单 #{order_id} → {status}")

    def _try_status_sync(self, order_id: int, status: str):
        """尝试同步状态到后端，失败则加入离线队列。"""
        endpoint_map = {"accepted": "accept_order", "abandoned": "abandon_order"}
        endpoint = endpoint_map.get(status, "")
        if not endpoint or not self.api_url:
            self._queue_status_sync(order_id, status)
            return
        try:
            resp = http_requests.post(
                f"{self.api_url}/api/{endpoint}",
                params={"token": self.token},
                json={"order_id": order_id},
                timeout=10,
            )
            if not resp.ok:
                self._queue_status_sync(order_id, status)
        except Exception:
            self._queue_status_sync(order_id, status)

    def sync_pending_statuses(self):
        """联网后重放离线状态同步队列。"""
        import json as _json
        path = self._status_queue_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                queue = _json.load(f)
        except Exception:
            queue = []
        if not queue:
            return
        self.status_message.emit(f"☁ 正在同步 {len(queue)} 条离线状态...")
        remaining = []
        for item in queue:
            endpoint_map = {"accepted": "accept_order", "abandoned": "abandon_order",
                          "sent": "print_success", "failed": "print_fail"}
            status = item["status"]
            order_id = item["order_id"]
            if status in ("sent", "failed"):
                # print_success/print_fail 通过 SocketIO 发送
                if self._sio and self._connected:
                    try:
                        self._sio.emit("print_success" if status == "sent" else "print_fail",
                                      {"task_id": order_id})
                    except Exception:
                        remaining.append(item)
                else:
                    remaining.append(item)
            else:
                endpoint = endpoint_map.get(status, "").replace("_order", "_order")
                try:
                    resp = http_requests.post(
                        f"{self.api_url}/api/{endpoint}",
                        params={"token": self.token},
                        json={"order_id": order_id},
                        timeout=10,
                    )
                    if not resp.ok:
                        remaining.append(item)
                except Exception:
                    remaining.append(item)
        if remaining:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(remaining, f, ensure_ascii=False, indent=2)
            self.status_message.emit(f"☁ {len(remaining)} 条状态同步失败，已重新暂存")
        else:
            os.remove(path)
            self.status_message.emit(f"☁ 离线状态同步完成 ({len(queue)} 条)")

    def report_page_range_truncated(self, task_id: int, original_range: str,
                                      effective_range: str, total_pages: int):
        """回报后端：某个任务的页码范围被截断。"""
        if self._sio and self._connected:
            try:
                self._sio.emit("page_range_truncated", {
                    "task_id": task_id,
                    "original_range": original_range,
                    "effective_range": effective_range,
                    "total_pages": total_pages,
                })
                self.status_message.emit(
                    f"☁ 已回报页码范围截断: #{task_id} {original_range} → {effective_range}"
                )
            except Exception as e:
                logger.warning(f"回报 page_range_truncated 失败: {e}")

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
                            # 自动开始下载
                            self.accept_task(task.task_id)
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

        @self._sio.on("analyze_page_count")
        def _on_analyze_page_count(data):
            file_id = data.get("file_id", "")
            file_name = data.get("file_name", "")
            download_url = data.get("download_url", "")
            if file_id and download_url:
                self.status_message.emit(f"☁ 收到页数分析请求: {file_name}")
                t = threading.Thread(
                    target=self._analyze_and_report_page_count,
                    args=(file_id, download_url, file_name),
                    daemon=True,
                )
                t.start()

        @self._sio.on("storage_config_updated")
        def _on_storage_config_updated(data):
            """后端推送：储存保留时间已更新 → 同步到本地缓存"""
            days = data.get("retention_days", None)
            hours = data.get("retention_hours", None)
            if days is not None and hours is not None:
                total = int(days) + (int(hours) / 24.0)
                new_retention = max(1, int(total + 0.5))  # 进位取整，最少保留 1 天
                old = self._CACHE_RETENTION_DAYS
                self._CACHE_RETENTION_DAYS = new_retention
                self.status_message.emit(f"📦 缓存保留时间已同步: {old}天 → {new_retention}天")
                # 立即按新规则清理
                self._cleanup_pdf_cache()

        @self._sio.on("clear_local_cache")
        def _on_clear_local_cache(data):
            """后端推送：管理员清空了服务器缓存 → 同步清空本地 PDF 缓存"""
            msg = data.get("message", "管理员清空缓存") if isinstance(data, dict) else str(data)
            self.status_message.emit(f"📦 收到清空指令: {msg}")
            removed = 0
            index = self._load_cache_index()
            for md5 in list(index.keys()):
                pdf_path = os.path.join(self._cache_dir, f"{md5}.pdf")
                if os.path.isfile(pdf_path):
                    try:
                        os.remove(pdf_path)
                        removed += 1
                    except OSError:
                        pass
            if os.path.exists(self._cache_index_path()):
                try:
                    os.remove(self._cache_index_path())
                except OSError:
                    pass
            self.status_message.emit(f"📦 已清空本地 PDF 缓存 ({removed} 个文件)")

        @self._sio.on("order_canceled")
        def _on_order_canceled(data):
            order_id = int(data.get("order_id", 0)) if isinstance(data, dict) else 0
            task_ids = data.get("task_ids", []) if isinstance(data, dict) else []
            self.status_message.emit(f"☁ 订单 #{order_id} 已被用户取消")
            self.order_canceled.emit(order_id, task_ids)
            # 从待处理列表中移除对应任务
            for tid in task_ids:
                self._pending_tasks.pop(tid, None)

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
        """后台下载文件。成功则 task.local_path 有值，status='ready'。
        若 source_md5 已在 PDF 缓存中，跳过下载直接使用缓存。"""
        task_id = task.task_id
        url = task.download_url

        if not url:
            task.status = "error"
            task.error_message = "缺少下载链接"
            self.task_updated.emit(task)
            return

        # 若后端已提供 source_md5 且本地 PDF 缓存已命中，跳过下载
        if task.source_md5:
            cached_pdf, cached_meta = self._get_cached_pdf(task.source_md5)
            if cached_pdf:
                task.local_path = cached_pdf
                task.status = "ready"
                task.download_progress = 100
                self.task_updated.emit(task)
                self.status_message.emit(
                    f"☁ 缓存命中 #{task_id}: {task.file_name} (MD5={task.source_md5[:8]}...，跳过下载)"
                )
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

            md5_hasher = hashlib.md5()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    md5_hasher.update(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        progress = int(downloaded / total * 100)
                        if progress != task.download_progress:
                            task.download_progress = progress
                            self.task_updated.emit(task)

            # 计算 MD5 存入任务（供后续缓存查找）
            source_md5 = md5_hasher.hexdigest()
            task.source_md5 = source_md5

            # 若为 PDF 且缓存尚无，存入本地 PDF 缓存
            ext = os.path.splitext(original_name)[1].lower()
            if ext == ".pdf":
                cached, _ = self._get_cached_pdf(source_md5)
                if not cached:
                    self._save_pdf_to_cache(source_md5, dest, original_name, ext,
                                            page_count=0)  # 页数在打印时确定

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

    # ── PDF 缓存（MD5 绑定，7 天自动清理）──

    _CACHE_DIR: str | None = None
    _CACHE_RETENTION_DAYS = 7

    @property
    def _cache_dir(self) -> str:
        if self._CACHE_DIR:
            return self._CACHE_DIR
        # 放在 local_print_tool 目录下
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_cache")
        os.makedirs(d, exist_ok=True)
        self._CACHE_DIR = d
        return d

    def _cache_index_path(self) -> str:
        return os.path.join(self._cache_dir, "index.json")

    def _load_cache_index(self) -> dict:
        path = self._cache_index_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_cache_index(self, index: dict):
        with open(self._cache_index_path(), "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def _compute_md5_file(self, file_path: str) -> str:
        md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                md5.update(chunk)
        return md5.hexdigest()

    def _get_cached_pdf(self, md5: str) -> tuple[str | None, dict | None]:
        """查找指定 MD5 的缓存 PDF。返回 (pdf_path, metadata) 或 (None, None)。"""
        pdf_path = os.path.join(self._cache_dir, f"{md5}.pdf")
        if os.path.isfile(pdf_path):
            index = self._load_cache_index()
            meta = index.get(md5, {})
            return pdf_path, meta
        return None, None

    def _save_pdf_to_cache(self, md5: str, pdf_path: str, original_name: str, source_ext: str, page_count: int = 0):
        """将 PDF 文件存入缓存并更新索引。"""
        dest = os.path.join(self._cache_dir, f"{md5}.pdf")
        if not os.path.samefile(pdf_path, dest) if os.path.exists(dest) else True:
            shutil.copy2(pdf_path, dest)
        index = self._load_cache_index()
        index[md5] = {
            "original_name": original_name,
            "source_ext": source_ext,
            "page_count": page_count,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_cache_index(index)
        self.status_message.emit(f"📦 PDF 已缓存: {original_name} (MD5={md5[:8]}...)")
        self._schedule_cache_cleanup()

    def _cleanup_pdf_cache(self):
        """清理过期的缓存 PDF（超过保留天数）。"""
        cutoff = datetime.now() - timedelta(days=self._CACHE_RETENTION_DAYS)
        index = self._load_cache_index()
        removed = 0
        for md5, meta in list(index.items()):
            created_str = meta.get("created_at", "")
            try:
                created = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S")
                if created < cutoff:
                    pdf_path = os.path.join(self._cache_dir, f"{md5}.pdf")
                    if os.path.isfile(pdf_path):
                        os.remove(pdf_path)
                    del index[md5]
                    removed += 1
            except (ValueError, OSError):
                pass
        if removed > 0:
            self._save_cache_index(index)
            self.status_message.emit(f"📦 已清理 {removed} 个过期缓存 PDF")

    _cache_cleanup_scheduled = False

    def _schedule_cache_cleanup(self):
        """延迟调度一次缓存清理（避免每次存文件都清理）。"""
        if self._cache_cleanup_scheduled:
            return
        self._cache_cleanup_scheduled = True
        def _do_cleanup():
            time.sleep(5)  # 等当前操作完成
            self._cleanup_pdf_cache()
            self._cache_cleanup_scheduled = False
        t = threading.Thread(target=_do_cleanup, daemon=True)
        t.start()

    # ── 页数分析 ──

    def _analyze_and_report_page_count(self, file_id: str, download_url: str, file_name: str):
        """后台：下载 → MD5 查缓存 → 转换 PDF（如需） → 统计页数 → 回报后端。
        PDF 缓存由 MD5 索引，同文件再次上传时直接复用缓存 PDF，避免重复转换。"""
        ext = os.path.splitext(file_name)[1].lower()
        temp_dl: str = ""
        temp_pdf: str | None = None

        try:
            # 1. 下载文件到临时路径
            self.status_message.emit(f"☁ 页数分析: 下载 {file_name} ...")
            temp_dl = os.path.join(tempfile.gettempdir(), f"hn_analyze_{file_id}{ext}")
            resp = http_requests.get(download_url, timeout=120, stream=True)
            resp.raise_for_status()
            with open(temp_dl, "wb") as wf:
                for chunk in resp.iter_content(chunk_size=65536):
                    wf.write(chunk)

            # 2. 计算源文件 MD5，查缓存
            source_md5 = self._compute_md5_file(temp_dl)
            cached_pdf, cached_meta = self._get_cached_pdf(source_md5)

            if cached_pdf and cached_meta:
                # 缓存命中：直接统计页数
                self.status_message.emit(f"📦 缓存命中: {file_name} → {cached_meta.get('page_count', '?')} 页")
                from pdf_printer import get_pdf_info
                info = get_pdf_info(cached_pdf)
                page_count = info.get("page_count", 0)
                orientation = info.get("orientation", "")
                if page_count > 0:
                    self._report_page_count(file_id, file_name, page_count, orientation)
                    return

            # 3. 确定是否需要转换（优先 Word/WPS COM，降级 LibreOffice 子进程）
            if ext in (".doc", ".docx"):
                self.status_message.emit(f"☁ 页数分析: 转换 {file_name} → PDF ...")
                try:
                    from converter import get_converter
                    converter = get_converter()
                    temp_pdf = converter.convert(temp_dl)
                except Exception:
                    # COM 不可用时降级 LibreOffice
                    temp_pdf = self._convert_via_libreoffice(temp_dl)
                if temp_pdf and os.path.isfile(temp_pdf):
                    from pdf_printer import get_pdf_info
                    info = get_pdf_info(temp_pdf)
                    page_count = info.get("page_count", 0)
                    self._save_pdf_to_cache(source_md5, temp_pdf, file_name, ext, page_count)
                    if page_count > 0:
                        self._report_page_count(file_id, file_name, page_count, info.get("orientation", ""))
                    else:
                        self.status_message.emit(f"☁ 页数分析失败: 转换后无法读取 {file_name} 页数")
                else:
                    self.status_message.emit(f"☁ 页数分析失败: 无法转换 {file_name}")
            elif ext == ".pdf":
                from pdf_printer import get_pdf_info
                info = get_pdf_info(temp_dl)
                page_count = info.get("page_count", 0)
                if page_count > 0:
                    self._report_page_count(file_id, file_name, page_count, info.get("orientation", ""))
                else:
                    self.status_message.emit(f"☁ 页数分析失败: 无法读取 {file_name} 页数")
            else:
                self._report_page_count(file_id, file_name, 1, "")

        except Exception as e:
            self.status_message.emit(f"☁ 页数分析失败 ({file_name}): {e}")
            logger.warning(f"页数分析失败 ({file_name}): {e}")

        finally:
            for p in (temp_dl, temp_pdf):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    @staticmethod
    def _convert_via_libreoffice(input_path: str) -> str | None:
        """通过 LibreOffice 子进程将文档转为 PDF（线程安全，不依赖 COM/Qt）。
        返回输出 PDF 路径，失败返回 None。"""
        import subprocess
        outdir = os.path.dirname(input_path)
        try:
            result = subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", outdir, input_path],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                logger.warning(f"LibreOffice 转换失败 (rc={result.returncode})")
                return None
            base = os.path.splitext(os.path.basename(input_path))[0]
            pdf_path = os.path.join(outdir, base + ".pdf")
            if os.path.isfile(pdf_path):
                return pdf_path
            logger.warning(f"LibreOffice 未生成预期 PDF: {pdf_path}")
            return None
        except FileNotFoundError:
            logger.warning("LibreOffice (soffice) 未安装")
            return None
        except subprocess.TimeoutExpired:
            logger.warning("LibreOffice 转换超时")
            return None
        except Exception as e:
            logger.warning(f"LibreOffice 转换异常: {e}")
            return None

    def _report_page_count(self, file_id: str, file_name: str, page_count: int, orientation: str):
        """回报页数分析结果到后端。"""
        self.status_message.emit(f"☁ ✓ 页数分析完成: {file_name} → {page_count} 页 ({orientation})")
        if self._sio and self._connected:
            self._sio.emit("page_count_result", {
                "file_id": file_id,
                "page_count": page_count,
                "orientation": orientation,
                "success": True,
            })
            self.status_message.emit(f"☁ 已回报页数: {file_name} = {page_count} 页")
