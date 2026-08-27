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
import random
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import requests as http_requests

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# ---------- 常量 ----------

HEARTBEAT_INTERVAL = 30
RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 120
STATUS_QUEUE_MAX = 5000        # status_queue.json 队列上限，超出丢弃最旧条目
MAX_AUTH_FAIL_RETRIES = 6      # auth_fail 最大自动重连次数，之后停止并保持提示


def pdf_cache_key(source_md5: str, image_orientation: str = "", engine: str = "") -> str:
    """PDF 缓存 key：图片显式方向（landscape/portrait）时加方向后缀；docx 转换引擎
    （word/wps）不同渲染结果不同，必须按引擎分开缓存，否则改引擎后仍命中旧引擎 PDF
    （WPS 特殊布局被 Word 引擎转换的错误 PDF 会被永久复用）。其余用纯 MD5（向后兼容既有缓存）。"""
    parts = source_md5
    if image_orientation in ("landscape", "portrait"):
        parts = f"{parts}_{image_orientation}"
    if engine in ("word", "wps"):
        parts = f"{parts}_e{engine}"
    return parts


def get_cached_pdf_path(source_md5: str, image_orientation: str = "") -> str | None:
    """模块级函数：检查 PDF 缓存（供 PrintWorker 使用）。
    返回缓存 PDF 路径，无缓存返回 None。"""
    if not source_md5:
        return None
    import paths as _paths
    cache_dir = _paths.pdf_cache_dir()
    pdf_path = os.path.join(cache_dir, pdf_cache_key(source_md5, image_orientation) + ".pdf")
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
        "image_orientation",
        "delivery_enabled", "delivery_location", "urgency", "cover_page", "cover_page_price",
        "owner_name", "is_admin_print",
        # 2026-12：顾客订单标签页归属 = 下单用户绑定的成员名（后端从收支清算成员绑定反查）
        "bound_owner_name",
        "auto_print",
        # 无障碍打印预约（此前 __init__ 已赋值但这些字段漏在 __slots__ 之外 → 构造必报 AttributeError）
        "schedule_mode", "scheduled_at", "scheduled_ts", "schedule_frozen",
    )

    def __init__(self, data: dict):
        options = data.get("options", {})
        self.task_id: int = data.get("task_id", data.get("id", 0))
        self.order_id: int | None = data.get("order_id")
        self.order_number: str = data.get("order_number", "")
        self.file_name: str = data.get("file_name", data.get("file", ""))
        self.copies: int = int(options.get("copies", data.get("copies", 1)) or 0)  # P1: or 0 防御空值
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
        # 图片打印方向（仅图片文件有意义）：auto | landscape | portrait
        self.image_orientation: str = options.get("image_orientation", data.get("image_orientation", "auto")) or "auto"
        # 附加服务（来自前端订单配置，传递给本地标签页）
        self.delivery_enabled: bool = bool(data.get("delivery_enabled", False))
        self.delivery_location: str = data.get("delivery_location", "") or ""
        self.urgency: str = data.get("urgency", "低") or "低"
        self.cover_page: bool = bool(data.get("cover_page", False))
        self.cover_page_price: float = float(data.get("cover_page_price", 0.10) or 0.10)
        # v24.1：订单归属（管理员自行打印标记随任务下发，本地标签页预勾选）
        self.owner_name: str = data.get("owner_name", "") or ""
        self.is_admin_print: bool = bool(data.get("is_admin_print", False))
        # v5.24：顾客订单标签页的默认归属 = 下单用户绑定的成员名（后端反查成员绑定表）
        self.bound_owner_name: str = data.get("bound_owner_name", "") or ""
        self.auto_print: bool = bool(data.get("auto_print", False))  # 无障碍打印
        # 无障碍打印预约形式：now | at | countdown（'now' 即立即开始）
        self.schedule_mode: str = data.get("schedule_mode", "now") or "now"
        self.scheduled_at: str = data.get("scheduled_at", "") or ""     # 绝对时间 "%Y-%m-%d %H:%M:%S"
        self.scheduled_ts: int = int(data.get("scheduled_ts", 0) or 0)  # epoch 秒，本地到点自触发
        self.schedule_frozen: bool = bool(data.get("schedule_frozen", False))

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
            "source_md5": self.source_md5,
            "image_orientation": self.image_orientation,
            "owner_name": self.owner_name,
            "is_admin_print": self.is_admin_print,
            "bound_owner_name": self.bound_owner_name,
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
    start_print = Signal(int, int, list)  # 预约单到点/解除冻结：order_id, scheduled_ts, task_ids
    auth_failed = Signal(str)             # 认证失败（含消息），GUI 可连接此信号提示用户
    printer_state = Signal(object)        # 接单状态 dict（is_active/active_owner_name/...）— 2026-11

    def __init__(
        self,
        api_url: str = "",
        ws_url: str = "",
        token: str = "",
        client_id: str = "",
        device_name: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.api_url = api_url
        self.ws_url = ws_url
        self.token = token
        # 设备（计算机）名称：随连接上报后端，用于设备注册表与「打印机已被 XXX 接管」提示
        self.device_name: str = (device_name or socket.gethostname()).strip()[:64]
        # P0-1: 同机多实例 client_id 冲突兜底 —— GUI 旧逻辑传入的是机器名（socket.gethostname()），
        # 同一台机器多开实例会共用同一 client_id 导致任务互抢。
        # 此处生成持久化后缀（优先 %APPDATA%\HN打印工具\.client_id，升级覆盖安装不丢失；
        # 兼容旧版应用目录 .client_id 并迁移），最终形如 "hostname-abcdef1234"。
        if not client_id or client_id == socket.gethostname():
            try:
                app_dir = os.path.dirname(os.path.abspath(__file__))
                user_dir = os.path.join(os.environ.get("APPDATA", ""), "HN打印工具") if os.environ.get("APPDATA") else ""
                user_file = os.path.join(user_dir, ".client_id") if user_dir else ""
                app_file = os.path.join(app_dir, ".client_id")
                suffix = ""
                for candidate in (user_file, app_file):
                    if not candidate:
                        continue
                    try:
                        if os.path.isfile(candidate):
                            with open(candidate, "r", encoding="utf-8") as f:
                                s = f.read().strip()
                            if s:
                                suffix = s
                                break
                    except OSError:
                        continue
                if not suffix:
                    suffix = uuid.uuid4().hex[:10]
                written = False
                if user_dir:
                    try:
                        os.makedirs(user_dir, exist_ok=True)
                        with open(user_file, "w", encoding="utf-8") as f:
                            f.write(suffix)
                        written = True
                    except OSError:
                        pass
                if not written:
                    with open(app_file, "w", encoding="utf-8") as f:
                        f.write(suffix)
                self.client_id = f"{socket.gethostname()}-{suffix}"
            except Exception as e:
                logger.warning(f"client_id 持久化失败，回退纯 hostname: {e}")
                self.client_id = socket.gethostname()
        else:
            self.client_id = client_id

        # 内部状态
        self._sio: object | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._heartbeat_timer: threading.Timer | None = None
        self._reconnect_count = 0
        self._auth_fail_count = 0          # auth_fail 已发生次数（退避重连用）
        self._auth_fail_stop = False       # auth_fail 达到最大重试次数后停止自动重连
        self._stability_timer: threading.Timer | None = None  # 连接稳定 N 秒后清零退避计数
        self._last_replay_attempt = 0.0    # status_queue 重放退避时间戳（60s 间隔）
        self._replay_lock = threading.Lock()  # 重放退避门闩锁，防 GUI/后台线程并发重复重放
        self._download_threads: list[threading.Thread] = []   # 记录下载线程，供 stop() join

        # 本地任务缓存
        self._pending_tasks: dict[int, CloudTask] = {}
        # P1: 下载并发上限 4（原 _download_lock 为死代码，从未使用）
        self._download_semaphore = threading.BoundedSemaphore(4)
        # 防滥用：页数分析并发上限 2（每个分析线程会下载完整文件并 COM 转换；
        # 防批量上传大文件时分析线程无界堆积拖垮本机，与 _download_semaphore 独立计）
        self._analyze_semaphore = threading.BoundedSemaphore(2)
        # 页数分析取消标记：file_id → True（后端 cancel_page_analysis 事件置位，
        # 分析线程在下载/转换各阶段检查后中止且不回报）
        self._analysis_abort: dict[str, bool] = {}
        self._cache_index_lock = threading.Lock()
        # 从本地持久化文件加载上次的保留时间（避免每次启动都从 7 天开始）
        self._load_retention()
        # 接单状态（2026-11）：True = 本机启用接单（唯一接管者，由 GUI 配置并同步后端）。
        # 连上后若本机配置为接单 → 自动向后端 claim（重启/断线重连后自动续接单）；
        # 被其他在线设备占用时 claim 被拒 → take_orders 复位为 False 并通知 GUI 弹窗。
        self.take_orders: bool = False
        # 最近一次后端下发的接单状态（printer_state 事件），GUI 可据此展示接管者
        self.last_printer_state: dict = {}

    # ── 公共 API ──

    def start(self):
        """启动云客户端（后台线程）。"""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("CloudClient 已在运行，忽略重复 start() 调用")
            return
        self._stop_event.clear()
        self._auth_fail_stop = False
        self._auth_fail_count = 0
        # P1: 启动时清理 %TEMP% 下超过 24h 的残留下载临时文件（崩溃遗留兜底）
        self._cleanup_old_temp_files()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cloud-client")
        self._thread.start()
        logger.info("CloudClient 已启动")

    def stop(self):
        """停止云客户端。"""
        self._stop_event.set()
        self._cancel_heartbeat()
        self._cancel_backoff_reset()
        if self._sio:
            try:
                self._sio.disconnect()
            except Exception:
                pass
            self._sio = None
        # P2: 设置停止事件后 join 主循环/下载线程（带超时，不无限等待）
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        for t in list(self._download_threads):
            if t and t.is_alive():
                t.join(timeout=1)
        self._download_threads.clear()
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
        self._download_threads.append(t)
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

    def abandon_reserved_order(self, order_number: str, total_price: float = 0.0):
        """放弃本地预留订单（仅获取了订单号但未提交打印）。
        调用后端 /api/abandon_reserved_order，将 reserved → abandoned，并补记价格。"""
        if not self.api_url or not self.token or not order_number:
            return
        try:
            payload = {"order_number": order_number}
            if total_price > 0:
                payload["total_price"] = round(total_price, 2)
            resp = http_requests.post(
                f"{self.api_url}/api/abandon_reserved_order",
                params={"token": self.token},
                json=payload,
                timeout=10,
            )
            if resp.ok:
                self.status_message.emit(f"☁ 预留订单 {order_number} 已标记为放弃")
        except Exception:
            pass  # 离线时静默跳过，后端定时任务兜底

    def accept_order_to_server(self, order_id: int) -> str:
        """确认接受订单：调用后端 API。
        返回三态：
        - 'ok'       后端接受成功
        - 'canceled' 业务拒绝（订单已被用户取消等，重放也不会成功，不入离线队列）
        - 'offline'  网络失败/离线，已加入离线队列，联网后重放
        P2-9：accept 前由后端权威校验订单是否仍有效（canceled → 409），
        本地据此撤回已添加的标签页，避免打印已取消订单。"""
        if not self.api_url or not self.token:
            self._queue_status_sync(order_id, "accepted")
            return "offline"
        try:
            resp = http_requests.post(
                f"{self.api_url}/api/accept_order",
                params={"token": self.token},
                json={"order_id": order_id},
                timeout=10,
            )
        except Exception:
            self._queue_status_sync(order_id, "accepted")
            return "offline"
        if resp.ok:
            self.status_message.emit(f"☁ 订单 #{order_id} 已确认接受")
            return "ok"
        if 400 <= resp.status_code < 500:
            # 业务拒绝（如订单已取消）：不入离线队列，按不可接受处理
            self.status_message.emit(f"☁ 订单 #{order_id} 接受被拒绝: {(resp.text or '')[:120]}")
            return "canceled"
        self._queue_status_sync(order_id, "accepted")
        return "offline"

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

    def report_success(self, task_id: int, config: dict | None = None):
        """上报打印成功到云端（可附带实际打印配置，供后端同步 order_files）。
        断线或发送失败时暂存离线队列，联网后补报。"""
        payload = {"task_id": task_id}
        if config:
            payload.update(config)
        if self._sio and self._connected:
            try:
                self._sio.emit("print_success", payload)
                self.status_message.emit(f"☁ 任务 #{task_id} 上报: 打印成功")
                return
            except Exception as e:
                # P0-4: 在线上报 emit 异常也走离线队列兜底，避免状态丢失
                logger.warning(f"上报 print_success 失败，转入离线队列: {e}")
        # python-socketio 的 emit 无确认回调，无法感知服务器是否收到，只能检测抛异常
        self._queue_status_sync(0, "sent", task_id=task_id)

    def report_fail(self, task_id: int, error: str):
        """上报打印失败到云端。断线或发送失败时暂存离线队列，联网后补报。"""
        if self._sio and self._connected:
            try:
                self._sio.emit("print_fail", {"task_id": task_id, "error": error})
                self.status_message.emit(f"☁ 任务 #{task_id} 上报: 打印失败 — {error}")
                return
            except Exception as e:
                # P0-4: 同 report_success，发送失败转入离线队列兜底
                logger.warning(f"上报 print_fail 失败，转入离线队列: {e}")
        self._queue_status_sync(0, "failed", task_id=task_id)

    # ── 预约打印上报 ──

    def _sio_emit(self, event: str, data: dict) -> bool:
        """线程安全的 SocketIO 事件发送（断线静默返回 False）。"""
        sio = self._sio
        if sio is None or not self._connected:
            return False
        try:
            sio.emit(event, data)
            return True
        except Exception as e:
            logger.warning(f"emit {event} 失败: {e}")
            return False

    def _read_local_log_tail(self, max_bytes: int = 200 * 1024) -> str:
        """读取本机日志文件（%APPDATA%\\HN打印工具\\logs\\local_tool.log）尾部，供云端日志收集回报。
        v4.5 起日志统一存用户数据目录（paths.logs_dir()），与程序安装目录解耦；
        从倒数 max_bytes 字节处开始，跳过可能截断的半行再读取；失败返回空串。"""
        import paths as _paths
        log_path = os.path.join(_paths.logs_dir(), "local_tool.log")
        try:
            size = os.path.getsize(log_path)
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                    f.readline()  # 丢弃半行，保证从完整行开始
                return f.read()[-max_bytes:]
        except OSError:
            return ""

    def report_file_ready(self, order_id: int, task_ids: list):
        """预约单阶段①文件下载完成 → 后端 downloading→waiting（冻结时自动解除）。"""
        if not order_id or not task_ids:
            return
        if self._sio_emit("file_ready", {"order_id": order_id, "task_ids": task_ids}):
            self.status_message.emit(f"☁ 预约单 #{order_id} 文件就绪已上报")

    def report_download_delayed(self, order_id: int, task_ids: list):
        """预约单到点文件未就绪 → 后端冻结订单（暂停倒计时）。"""
        if not order_id:
            return
        if self._sio_emit("download_delayed", {"order_id": order_id, "task_ids": task_ids}):
            self.status_message.emit(f"☁ 预约单 #{order_id} 到点未就绪，已上报冻结")

    def report_start_printing(self, order_id: int, task_ids: list):
        """预约单到点开始打印 → 后端 waiting→printing（启用 3 分钟超时兜底）。"""
        if not order_id or not task_ids:
            return
        if self._sio_emit("start_printing", {"order_id": order_id, "task_ids": task_ids}):
            self.status_message.emit(f"☁ 预约单 #{order_id} 已到点开始打印")

    # ── 离线状态同步 ──

    def _status_queue_path(self) -> str:
        import paths as _paths
        d = _paths.pdf_cache_dir()
        return os.path.join(d, "status_queue.json")

    def _quarantine_status_queue(self, path: str):
        """status_queue.json 损坏时改名保留（不静默清空），从空队列继续。"""
        try:
            corrupt = f"{path}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            os.replace(path, corrupt)
            logger.error(f"status_queue.json 损坏，已改名保留为 {corrupt}，从空队列继续")
        except OSError as e:
            logger.error(f"status_queue.json 损坏且无法改名保留: {e}")

    def _queue_status_sync(self, order_id: int, status: str, task_id: int = 0):
        """离线时将状态变更暂存到本地队列。task_id 供 sent/failed 回放时精确定位子任务。"""
        import json as _json
        path = self._status_queue_path()
        queue = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    queue = _json.load(f)
                if not isinstance(queue, list):
                    raise ValueError("队列结构异常")
            except Exception:
                # P0-3: 损坏文件改名保留，不从空队列静默覆盖
                self._quarantine_status_queue(path)
                queue = []
        item = {"order_id": order_id, "status": status,
                "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if task_id:
            item["task_id"] = task_id
        queue.append(item)
        # P2: 队列无上限保护 —— 超过 STATUS_QUEUE_MAX 条时丢弃最旧
        if len(queue) > STATUS_QUEUE_MAX:
            dropped = len(queue) - STATUS_QUEUE_MAX
            queue = queue[dropped:]
            logger.warning(f"status_queue 超过 {STATUS_QUEUE_MAX} 条，丢弃最旧 {dropped} 条")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # P0-3: tmp + os.replace 原子写，避免截断式写入损坏 JSON
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(queue, f, ensure_ascii=False, indent=2)
                f.flush()
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
        self.status_message.emit(f"☁ 状态同步已暂存: 订单 #{order_id} → {status}")

    def _try_status_sync(self, order_id: int, status: str) -> bool:
        """尝试同步状态到后端，失败则加入离线队列。返回是否成功。"""
        endpoint_map = {"accepted": "accept_order", "abandoned": "abandon_order"}
        endpoint = endpoint_map.get(status, "")
        if not endpoint or not self.api_url:
            self._queue_status_sync(order_id, status)
            return False
        try:
            resp = http_requests.post(
                f"{self.api_url}/api/{endpoint}",
                params={"token": self.token},
                json={"order_id": order_id},
                timeout=10,
            )
            if not resp.ok:
                if 400 <= resp.status_code < 500:
                    # 业务拒绝（如订单已取消）：不入队重放——重放也不会成功，避免死循环
                    logger.warning(f"状态同步 {status} 订单 #{order_id} 被业务拒绝"
                                   f" ({resp.status_code})，丢弃该条目")
                    return False
                self._queue_status_sync(order_id, status)
                return False
            return True
        except Exception:
            self._queue_status_sync(order_id, status)
            return False

    def sync_pending_statuses(self):
        """联网后重放离线状态同步队列。"""
        # P2: 重放退避 —— 距上次尝试不足 60s 时跳过（无论成败），
        # 避免每次重连/连接恢复都立刻重放造成循环刷后端；
        # 门闩加锁，防止 GUI（connection_changed 回调）与本模块连接线程并发重复重放
        with self._replay_lock:
            now = time.time()
            if now - self._last_replay_attempt < 60:
                return
            self._last_replay_attempt = now
        import json as _json
        path = self._status_queue_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                queue = _json.load(f)
            if not isinstance(queue, list):
                raise ValueError("队列结构异常")
        except Exception:
            # P0-3: 损坏文件改名保留，不从空队列静默覆盖
            self._quarantine_status_queue(path)
            queue = []
        if not queue:
            return
        self.status_message.emit(f"☁ 正在同步 {len(queue)} 条离线状态...")
        remaining = []
        for item in queue:
            endpoint_map = {"accepted": "accept_order", "abandoned": "abandon_order",
                          "sent": "print_success", "failed": "print_fail"}
            status = item.get("status", "")
            order_id = item.get("order_id", 0)
            if status not in endpoint_map:
                # P1: 未知状态/结构残缺的条目无法重放，直接丢弃，避免死循环
                logger.warning(f"status_queue 发现未知状态条目，已丢弃: {item}")
                continue
            if status in ("sent", "failed"):
                # P1: 无 task_id 的死条目（回退 order_id=0 会错报到错误任务）→ 清理丢弃
                if not item.get("task_id"):
                    logger.warning(f"status_queue 发现无 task_id 的死条目，已丢弃: {item}")
                    continue
                # print_success/print_fail 通过 SocketIO 发送（task_id 定位子任务）
                if self._sio and self._connected:
                    try:
                        task_id = item["task_id"]
                        self._sio.emit("print_success" if status == "sent" else "print_fail",
                                      {"task_id": task_id})
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
                        if 400 <= resp.status_code < 500:
                            # 业务拒绝（如订单已取消）：丢弃，不反复重放
                            logger.warning(f"离线状态同步 {status} 订单 #{order_id} 被业务拒绝"
                                           f" ({resp.status_code})，丢弃该条目")
                            continue
                        remaining.append(item)
                except Exception:
                    remaining.append(item)
        if remaining:
            # P0-3: 剩余条目原子写回
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    _json.dump(remaining, f, ensure_ascii=False, indent=2)
                    f.flush()
                os.replace(tmp, path)
            except Exception:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            self.status_message.emit(f"☁ {len(remaining)} 条状态同步失败，已重新暂存")
        else:
            try:
                os.remove(path)
            except OSError:
                logger.warning("删除已同步的 status_queue.json 失败")
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
                        try:
                            task = CloudTask(item)
                        except (TypeError, ValueError, AttributeError) as e:
                            # P1: 单条数据解析失败不拖垮整个拉取（AttributeError 兜底 __slots__ 遗漏）
                            logger.error(f"解析拉取的任务数据失败，已跳过: {e}")
                            continue
                        if task.task_id not in self._pending_tasks:
                            self._pending_tasks[task.task_id] = task
                            self.task_received.emit(task)
                            self.status_message.emit(
                                f"☁ HTTP 拉取到任务 #{task.task_id}: {task.file_name}"
                            )
                            # 自动开始下载
                            self.accept_task(task.task_id)
        except Exception as e:
            # P2: 拉取失败不再是 debug 级别 —— 需在日志中可见
            logger.warning(f"HTTP 拉取排队任务失败: {e}")

    # ── 接单接管（2026-11：多设备共连时仅启用接单的设备接收订单）──

    def claim_printer(self, owner_name: str = "") -> tuple[bool, str, dict]:
        """启用接单：请求后端将本机设为唯一接管者。
        返回 (成功?, 消息, 附加信息 dict)。被其他在线设备占用时成功=False，
        消息含「打印机已被 XXX 接管」，附加信息含 holder_name/holder_owner 供弹窗展示。"""
        if not self.api_url or not self.token:
            return False, "云端未配置，无法启用接单", {}
        try:
            resp = http_requests.post(
                f"{self.api_url}/api/printer/claim",
                params={"token": self.token},
                json={"client_id": self.client_id, "device_name": self.device_name,
                      "owner_name": owner_name or ""},
                timeout=10,
            )
            try:
                data = resp.json()
            except Exception:
                data = {}
            if resp.ok and data.get("success"):
                self.take_orders = True
                self.status_message.emit(f"☁ 接单已启用（本机为唯一接单设备）")
                return True, data.get("message", "接单已启用"), data
            msg = data.get("message", "") if isinstance(data, dict) else ""
            return False, msg or f"接单启用失败（HTTP {resp.status_code}）", data or {}
        except Exception as e:
            return False, f"接单启用失败: {e}", {}

    def release_printer(self) -> tuple[bool, str]:
        """关闭接单：释放本机的接管者身份。"""
        if not self.api_url or not self.token:
            return False, "云端未配置"
        try:
            resp = http_requests.post(
                f"{self.api_url}/api/printer/release",
                params={"token": self.token},
                json={"client_id": self.client_id},
                timeout=10,
            )
            try:
                data = resp.json()
            except Exception:
                data = {}
            if resp.ok and data.get("success"):
                self.take_orders = False
                self.status_message.emit("☁ 接单已关闭")
                return True, data.get("message", "接单已关闭")
            return False, (data.get("message", "") if isinstance(data, dict) else "") or "关闭接单失败"
        except Exception as e:
            return False, f"关闭接单失败: {e}"

    def get_printer_devices(self) -> dict:
        """拉取设备列表（计算机名/在线状态/所有者/是否接单），失败返回空 dict。"""
        if not self.api_url or not self.token:
            return {}
        try:
            resp = http_requests.get(
                f"{self.api_url}/api/printer/devices",
                params={"token": self.token},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                if isinstance(data, dict) and data.get("success"):
                    return data
            return {}
        except Exception as e:
            logger.warning(f"拉取设备列表失败: {e}")
            return {}

    def bind_owner(self, owner_name: str) -> tuple[bool, str]:
        """绑定本设备所有者（收支成员姓名）。"""
        if not self.api_url or not self.token:
            return False, "云端未配置"
        try:
            resp = http_requests.post(
                f"{self.api_url}/api/printer/bind_owner",
                params={"token": self.token},
                json={"client_id": self.client_id, "owner_name": owner_name or ""},
                timeout=10,
            )
            try:
                data = resp.json()
            except Exception:
                data = {}
            if resp.ok and data.get("success"):
                return True, data.get("message", "所有者已保存")
            return False, (data.get("message", "") if isinstance(data, dict) else "") or "保存失败"
        except Exception as e:
            return False, f"保存失败: {e}"

    def _post_connect_sync(self):
        """连接成功后的异步收尾（独立线程，不阻塞 socketio 读循环）：
        先重放离线状态队列，再拉取排队任务（先重放后拉取保证状态一致）。
        2026-11：若本机配置了接单 → 自动向后端续接单（重启/重连后保持接管身份）。"""
        try:
            self.sync_pending_statuses()
        except Exception as e:
            logger.warning(f"离线状态重放异常: {e}")
        # 接单续期：本机配置为接单时，连接后自动 claim（接管者掉线期间被他人接管会被拒，
        # 此时复位 take_orders 并通知 GUI 弹窗提示「打印机已被 XXX 接管」）
        if getattr(self, "take_orders", False):
            try:
                ok, msg, extra = self.claim_printer()
                if not ok:
                    self.take_orders = False
                    self.status_message.emit(f"☁ 接单续期失败: {msg}")
                    self.printer_state.emit({
                        "is_active": False,
                        "take_orders_rejected": True,
                        "message": msg,
                        "holder_name": extra.get("holder_name", "") if isinstance(extra, dict) else "",
                        "holder_owner": extra.get("holder_owner", "") if isinstance(extra, dict) else "",
                    })
            except Exception as e:
                logger.warning(f"接单续期异常: {e}")
        try:
            self.pull_pending()
        except Exception as e:
            logger.warning(f"拉取排队任务异常: {e}")

    # ── 内部实现 ──

    def _run_loop(self):
        """后台主循环：连接 → 维持 → 重连。"""
        while not self._stop_event.is_set():
            if self._auth_fail_stop:
                # P1: auth_fail 达到最大重试次数，停止自动重连并保持提示（不再静默死亡）
                self.status_message.emit(
                    "☁ 认证失败已达最大重试次数，已停止自动重连（请检查 token 后重启工具）")
                break
            try:
                self._connect_and_wait()
            except Exception as e:
                logger.warning(f"CloudClient 连接异常: {e}")
            if not self._stop_event.is_set():
                if self._auth_fail_count > 0:
                    # auth_fail 场景：重连间隔 10s 起、封顶 60s（带小抖动防同步重试）
                    delay = min(
                        10 * (2 ** (self._auth_fail_count - 1)) * (1 + random.random() * 0.3), 60)
                else:
                    # 普通断线：指数退避 + 随机抖动，避免多实例惊群同时重连
                    delay = min(RECONNECT_BASE_DELAY * (2 ** self._reconnect_count)
                                * (1 + random.random()), RECONNECT_MAX_DELAY)
                self._reconnect_count += 1
                self.status_message.emit(f"☁ {int(delay)}s 后重连...")
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
            self.connection_changed.emit(True)
            self.status_message.emit("☁ 已连接到云端服务器")
            self._start_heartbeat()
            # P1: 连接稳定 10s 后才清零退避计数，避免"连上即断"循环中退避被反复重置
            self._schedule_backoff_reset()
            # P2: 同步的 pull_pending 有 10s 超时，会阻塞 socketio 读循环 → 独立线程执行；
            # 线程内先重放离线状态队列、再拉取排队任务（先重放后拉取保证状态一致）
            threading.Thread(target=self._post_connect_sync, daemon=True,
                             name="cloud-post-connect").start()
            connect_event.set()

        @self._sio.on("disconnect")
        def _on_disconnect():
            self._connected = False
            self.connection_changed.emit(False)
            self.status_message.emit("☁ 已断开云端连接")
            self._cancel_heartbeat()
            self._cancel_backoff_reset()
            connect_event.set()

        @self._sio.on("print_task")
        def _on_print_task(data):
            try:
                task = CloudTask(data)
            except (TypeError, ValueError, AttributeError) as e:
                # P1: 单条推送数据解析失败不崩溃，跳过该任务（AttributeError 兜底 __slots__ 遗漏）
                logger.error(f"解析推送的任务数据失败，已跳过: {e}")
                self.status_message.emit(f"☁ 收到无法解析的云任务，已跳过: {e}")
                return
            if task.task_id not in self._pending_tasks:
                self._pending_tasks[task.task_id] = task
                self.task_received.emit(task)
                self.status_message.emit(
                    f"☁ 收到云任务 #{task.task_id}: {task.file_name} "
                    f"({task.copies}份, {'双面' if task.duplex == 'on' else '单面'})"
                )
                # 自动开始下载
                self.accept_task(task.task_id)

        @self._sio.on("start_print")
        def _on_start_print(data):
            """预约单到点 / 解除冻结：后端发来开始打印指令（含新的目标时间）。
            本地通常已按 scheduled_ts 自触发，此信号用于冻结解除/兜底，GUI 侧幂等处理。"""
            order_id = data.get("order_id")
            task_ids = data.get("task_ids") or []
            if isinstance(task_ids, int):
                task_ids = [task_ids]
            scheduled_ts = int(data.get("scheduled_ts", 0) or 0)
            self.start_print.emit(order_id, scheduled_ts, [int(t) for t in task_ids])
            self.status_message.emit(f"☁ 预约单 #{order_id} 收到开始打印指令"
                                     + (f"（目标 {scheduled_ts}）" if scheduled_ts else ""))

        @self._sio.on("analyze_page_count")
        def _on_analyze_page_count(data):
            file_id = data.get("file_id", "")
            file_name = data.get("file_name", "")
            download_url = data.get("download_url", "")
            source_md5 = data.get("source_md5", "") or ""  # 后端携带 MD5 → 本地可先查缓存免下载
            if file_id and download_url:
                self.status_message.emit(f"☁ 收到页数分析请求: {file_name}")
                self._spawn_analysis_thread(file_id, download_url, file_name, source_md5)

        @self._sio.on("cancel_page_analysis")
        def _on_cancel_page_analysis(data):
            file_id = data.get("file_id", "")
            if file_id:
                self._analysis_abort[file_id] = True
                self.status_message.emit(f"⛔ 已请求取消页数分析: {file_id[:8]}...")

        @self._sio.on("storage_config_updated")
        def _on_storage_config_updated(data):
            """后端推送：储存保留时间已更新 → 同步到本地缓存（小时级精度）。
            仅当与本地持久化值不同时记录日志，避免每次重连都刷屏。"""
            days = data.get("retention_days", None)
            hours = data.get("retention_hours", None)
            if days is not None and hours is not None:
                new_hours = int(days) * 24 + int(hours)
                old_hours = self._CACHE_RETENTION_HOURS
                if new_hours == old_hours:
                    return  # 未变化，跳过日志和清理
                self._CACHE_RETENTION_HOURS = new_hours
                # 持久化到本地文件，下次启动直接恢复此值
                self._save_retention(new_hours)
                # 友好显示
                if new_hours >= 24 and new_hours % 24 == 0:
                    display = f"{new_hours // 24}天"
                else:
                    display = f"{new_hours}小时"
                old_display = f"{old_hours // 24}天" if old_hours >= 24 and old_hours % 24 == 0 else f"{old_hours}小时"
                self.status_message.emit(f"📦 缓存保留时间已同步: {old_display} → {display}")
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

        @self._sio.on("printer_state")
        def _on_printer_state(data):
            """后端下发接单状态：本机是否接管、当前接管者（计算机名/所有者）。"""
            try:
                payload = dict(data or {})
            except Exception:
                payload = {}
            self.last_printer_state = payload
            self.printer_state.emit(payload)
            if payload.get("is_active"):
                self.status_message.emit("☁ 本机为当前接单设备（可接收云端订单）")
            elif payload.get("active_client_id"):
                holder = payload.get("active_device_name") or payload.get("active_client_id", "")
                owner = payload.get("active_owner_name", "")
                self.status_message.emit(
                    f"☁ 当前接单设备: {holder}" + (f"（所有者：{owner}）" if owner else "")
                    + "，本机未接单")

        @self._sio.on("request_log")
        def _on_request_log(data):
            """后端收集在线设备日志：读取本机日志尾部并回报（logs 事件）。"""
            try:
                request_id = int((data or {}).get("request_id", 0) or 0)
                max_bytes = int((data or {}).get("max_bytes", 0) or 0)
            except (TypeError, ValueError):
                request_id, max_bytes = 0, 0
            content = self._read_local_log_tail(max_bytes or 200 * 1024)
            if self._sio_emit("logs", {"request_id": request_id, "content": content}):
                self.status_message.emit(f"📋 已回报本机日志（云端日志收集, {len(content)} 字节）")

        @self._sio.on("pong")
        def _on_pong():
            pass

        @self._sio.on("auth_fail")
        def _on_auth_fail(data):
            msg = data.get("message", "未知原因") if isinstance(data, dict) else str(data)
            self._auth_fail_count += 1
            logger.error(f"认证失败({self._auth_fail_count}/{MAX_AUTH_FAIL_RETRIES}): {msg}")
            self.status_message.emit(
                f"☁ 认证失败: {msg}（{self._auth_fail_count}/{MAX_AUTH_FAIL_RETRIES}，将按退避自动重连）")
            self.auth_failed.emit(msg)
            # P1: 不再 _stop_event.set() 静默停摆 —— 保持连接循环，
            # 由 _run_loop 按 10s→60s 退避自动重连，达上限后停止并保持提示
            if self._auth_fail_count >= MAX_AUTH_FAIL_RETRIES:
                self._auth_fail_stop = True
            # 服务端通常认证失败即断开；此处主动断开确保 _run_loop 进入退避重连
            try:
                if self._sio:
                    self._sio.disconnect()
            except Exception:
                pass

        # 连接
        connect_url = (f"{self.ws_url}?token={self.token}&client_id={self.client_id}"
                       f"&device_name={quote(self.device_name)}")
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

    # ── 退避计数稳定性重置 ──

    def _schedule_backoff_reset(self):
        """连接稳定 10s 后才清零退避计数（防止连上即断时退避被反复重置）。"""
        self._cancel_backoff_reset()
        t = threading.Timer(10.0, self._reset_backoff_if_stable)
        t.daemon = True
        self._stability_timer = t
        t.start()

    def _reset_backoff_if_stable(self):
        if self._connected:
            self._reconnect_count = 0
            logger.debug("连接已稳定 10s，退避计数已清零")
        self._stability_timer = None

    def _cancel_backoff_reset(self):
        if self._stability_timer:
            self._stability_timer.cancel()
            self._stability_timer = None

    # ── 临时文件清理 ──

    def _cleanup_old_temp_files(self):
        """启动时清理 %TEMP% 下残留超过 24h 的云端下载临时文件（hn_cloud_*/hn_analyze_*）。
        正常路径由下载逻辑删除/保留；此方法只兜底进程崩溃等遗留场景。"""
        cutoff = time.time() - 24 * 3600
        try:
            for name in os.listdir(tempfile.gettempdir()):
                if name.startswith("hn_cloud_") or name.startswith("hn_analyze_"):
                    p = os.path.join(tempfile.gettempdir(), name)
                    try:
                        if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                            os.remove(p)
                            logger.info(f"已清理过期临时文件: {name}")
                    except OSError:
                        pass
        except OSError as e:
            logger.warning(f"清理旧临时文件失败: {e}")

    # ── 文件下载 ──

    def _download_file(self, task: CloudTask):
        """后台下载文件。成功则 task.local_path 有值，status='ready'。
        若 source_md5 已在 PDF 缓存中，跳过下载直接使用缓存。
        P1: 下载并发上限由 _download_semaphore（BoundedSemaphore(4)）控制，
        无空闲槽位时在后台线程内等待，不阻塞 GUI。"""
        self._download_semaphore.acquire()
        try:
            self._download_file_inner(task)
        finally:
            self._download_semaphore.release()

    def _download_file_inner(self, task: CloudTask):
        """下载核心逻辑（MD5 校验重试、文件名净化、幽灵任务检查、失败清理）。"""
        task_id = task.task_id
        url = task.download_url

        if not url:
            task.status = "error"
            task.error_message = "缺少下载链接"
            self.task_updated.emit(task)
            return

        # 若后端已提供 source_md5 且本地 PDF 缓存已命中，跳过下载（图片按方向后缀分开缓存）。
        # docx 除外：转换引擎取决于文档内容（last editor），未下载无法确定 → 一律下载后判断，
        # 避免命中旧引擎（如 Word 渲染 WPS 特殊布局）的错误缓存。
        if task.source_md5:
            _dl_ext = os.path.splitext(task.file_name)[1].lower()
            if _dl_ext != ".docx":
                cached_pdf, cached_meta = self._get_cached_pdf(task.source_md5, task.image_orientation)
                if cached_pdf:
                    task.local_path = cached_pdf
                    task.status = "ready"
                    task.download_progress = 100
                    self.task_updated.emit(task)
                    self.status_message.emit(
                        f"☁ 缓存命中 #{task_id}: {task.file_name} (MD5={task.source_md5[:8]}...，跳过下载)"
                    )
                    self._report_file_ready_if_scheduled(task)
                    return

        dest: str = ""
        # P0-2: 后端 pull/push payload 若提供 source_md5，下载完成后比对校验
        # （后端需在 pull/push payload 中带上 source_md5 字段才能启用该校验）
        expected_md5 = task.source_md5
        try:
            self.status_message.emit(f"☁ 开始下载 #{task_id}: {task.file_name}")
            for attempt in range(2):
                if attempt > 0:
                    self.status_message.emit(f"☁ MD5 校验失败，重新下载 #{task_id}（第 2 次尝试）")
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
                # P1: 文件名净化 —— 只取 basename，Windows 非法字符与控制字符替换为 _
                original_name = os.path.basename(original_name) or f"cloud_task_{task_id}.dat"
                original_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", original_name)

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

                # 计算本地 MD5（校验通过后才写入 task，供后续缓存查找）
                source_md5 = md5_hasher.hexdigest()
                if expected_md5 and source_md5 != expected_md5:
                    # P0-2: 与后端声明不一致 —— 删除半截文件，重试一次
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                    if attempt == 0:
                        logger.warning(
                            f"任务 #{task_id} MD5 校验失败（期望 {expected_md5}，实际 {source_md5}），重试一次")
                        task.download_progress = 0
                        continue
                    raise ValueError(
                        f"MD5 校验失败: 期望 {expected_md5}，实际 {source_md5}（重试后仍不一致）")
                break

            task.source_md5 = source_md5

            # P1: 幽灵任务检查 —— 下载期间任务已被取消/打回（从 _pending_tasks 移除），
            # 不再触发 GUI 回调，并删除临时文件
            if task.task_id not in self._pending_tasks:
                try:
                    if os.path.isfile(dest):
                        os.remove(dest)
                except OSError:
                    pass
                logger.info(f"任务 #{task.task_id} 下载完成但已不在待处理列表（取消/打回），结果丢弃")
                return

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
            self._report_file_ready_if_scheduled(task)

        except Exception as e:
            task.status = "error"
            task.error_message = str(e)
            # P1: 失败路径删除残留的半截文件；成功路径文件保留给打印用，
            # 打印完成后由 GUI 侧负责删除（本文件无法感知打印完成时机）
            if dest:
                try:
                    if os.path.isfile(dest):
                        os.remove(dest)
                except OSError:
                    pass
            self.task_updated.emit(task)
            self.status_message.emit(f"☁ 下载失败 #{task.task_id}: {e}")

    def _report_file_ready_if_scheduled(self, task: CloudTask):
        """预约单文件下载完成 → 上报 file_ready（后端 downloading→waiting，冻结时自动解除）。
        断线时静默跳过，由本地冻结自恢复逻辑兜底。"""
        if task.order_id and getattr(task, "schedule_mode", "now") != "now":
            try:
                self.report_file_ready(task.order_id, [task.task_id])
            except Exception as e:
                logger.warning(f"上报 file_ready 异常: {e}")

    # ── PDF 缓存（MD5 绑定，默认 7 天自动清理）──

    _CACHE_DIR: str | None = None
    _CACHE_RETENTION_HOURS = 168  # 7天，默认为小时；0 = 永不过期

    def _retention_path(self) -> str:
        """持久化保留时间的侧边文件路径。"""
        return os.path.join(self._cache_dir, "retention.json")

    def _load_retention(self):
        """从本地文件加载上次保存的保留时间，避免每次启动从 7 天硬编码开始。"""
        path = os.path.join(self._cache_dir, "retention.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    val = int(data.get("retention_hours", 0))
                    if val > 0:
                        self._CACHE_RETENTION_HOURS = val
            except (OSError, ValueError, json.JSONDecodeError):
                pass

    def _save_retention(self, hours: int):
        """将保留时间持久化到本地文件，供下次启动恢复。"""
        path = os.path.join(self._cache_dir, "retention.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"retention_hours": hours}, f)
        except OSError:
            pass

    @property
    def _cache_dir(self) -> str:
        if self._CACHE_DIR:
            return self._CACHE_DIR
        # 缓存目录统一在 %APPDATA%\HN打印工具\pdf_cache（可重建，安装覆盖不丢数据）
        import paths as _paths
        d = _paths.pdf_cache_dir()
        self._CACHE_DIR = d
        return d

    def _cache_index_path(self) -> str:
        return os.path.join(self._cache_dir, "index.json")

    def _load_cache_index(self) -> dict:
        with self._cache_index_lock:
            path = self._cache_index_path()
            if not os.path.exists(path):
                return {}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}

    def _save_cache_index(self, index: dict):
        # 写入临时文件后原子重命名，避免截断式写入导致 JSON 损坏
        with self._cache_index_lock:
            path = self._cache_index_path()
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(index, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except Exception:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                raise

    def _compute_md5_file(self, file_path: str) -> str:
        md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                md5.update(chunk)
        return md5.hexdigest()

    def _get_cached_pdf(self, md5: str, image_orientation: str = "", engine: str = "") -> tuple[str | None, dict | None]:
        """查找指定 MD5 的缓存 PDF。返回 (pdf_path, metadata) 或 (None, None)。
        图片按方向后缀分开缓存（landscape/portrait）；docx 按转换引擎分开缓存（word/wps），
        避免引擎变更后仍命中旧引擎的错误 PDF。P0-2: 命中时校验文件头 %PDF。"""
        key = pdf_cache_key(md5, image_orientation, engine)
        pdf_path = os.path.join(self._cache_dir, f"{key}.pdf")
        if os.path.isfile(pdf_path):
            try:
                with open(pdf_path, "rb") as f:
                    header = f.read(5)
                if not header.startswith(b"%PDF"):
                    raise ValueError("损坏的 PDF 文件头")
            except (OSError, ValueError) as e:
                logger.warning(f"缓存 PDF 损坏，删除自愈: {pdf_path} ({e})")
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass
                index = self._load_cache_index()
                if key in index:
                    del index[key]
                    self._save_cache_index(index)
                return None, None
            index = self._load_cache_index()
            meta = index.get(key, {})
            return pdf_path, meta
        return None, None

    def remove_cached_pdf(self, source_md5: str, image_orientation: str = "", engine: str = ""):
        """删除指定 MD5（可含方向/引擎后缀）的缓存 PDF 及其索引条目（用于放弃订单时清理）。"""
        if not source_md5:
            return
        key = pdf_cache_key(source_md5, image_orientation, engine)
        pdf_path = os.path.join(self._cache_dir, f"{key}.pdf")
        if os.path.isfile(pdf_path):
            try:
                os.remove(pdf_path)
            except OSError:
                pass
        index = self._load_cache_index()
        if key in index:
            del index[key]
            self._save_cache_index(index)
            self.status_message.emit(f"📦 已清理缓存: {key[:8]}...")

    def _save_pdf_to_cache(self, md5: str, pdf_path: str, original_name: str, source_ext: str,
                           page_count: int = 0, image_orientation: str = "", engine: str = ""):
        """将 PDF 文件存入缓存并更新索引（图片按方向后缀、docx 按转换引擎分开缓存）。"""
        key = pdf_cache_key(md5, image_orientation, engine)
        dest = os.path.join(self._cache_dir, f"{key}.pdf")
        # P0-2: 原子写（tmp + os.replace），避免 copy2 截断式写入损坏缓存
        if not os.path.exists(dest) or not os.path.samefile(pdf_path, dest):
            tmp = dest + ".tmp"
            shutil.copy2(pdf_path, tmp)
            os.replace(tmp, dest)
        index = self._load_cache_index()
        index[key] = {
            "original_name": original_name,
            "source_ext": source_ext,
            "page_count": page_count,
            "engine": engine,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_cache_index(index)
        self.status_message.emit(f"📦 PDF 已缓存: {original_name} (MD5={md5[:8]}...)")
        self._schedule_cache_cleanup()

    def _cleanup_pdf_cache(self):
        """清理过期的缓存 PDF（超过保留小时数）。"""
        if self._CACHE_RETENTION_HOURS <= 0:
            return  # 0 = 永不过期
        cutoff = datetime.now() - timedelta(hours=self._CACHE_RETENTION_HOURS)
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

    def _spawn_analysis_thread(self, file_id: str, download_url: str, file_name: str, source_md5: str = ""):
        """以受限并发启动一个页数分析线程（防滥用：防分析线程无界堆积拖垮本机）。
        用独立信号量排队，等待者不占线程栈；超出的请求排队而非直接丢弃。"""
        def _worker():
            self._analyze_semaphore.acquire()
            try:
                self._analyze_and_report_page_count(file_id, download_url, file_name, source_md5)
            finally:
                self._analyze_semaphore.release()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        self._download_threads.append(t)  # 纳入 stop() 的 join 管理

    def _analyze_and_report_page_count(self, file_id: str, download_url: str, file_name: str,
                                       source_md5: str = ""):
        """后台：查缓存（后端 MD5 → 免下载） → 下载 → MD5 查缓存 → 转换 PDF → 统计页数 → 回报后端。
        支持取消：cancel_page_analysis 事件置位 _analysis_abort[file_id]，下载/转换各阶段检查后中止且不回报。"""
        ext = os.path.splitext(file_name)[1].lower()
        temp_dl: str = ""
        temp_pdf: str | None = None
        # 新一轮分析开始：清掉可能残留的取消标记（取消后同一 file_id 可能被重新发起）
        self._analysis_abort.pop(file_id, None)

        def _canceled() -> bool:
            return bool(self._analysis_abort.get(file_id))

        try:
            # 0. 后端已带 MD5 → 先查 PDF 缓存，命中则跳过下载（本地按云端 MD5 直接命中）
            if source_md5:
                cached_pdf, cached_meta = self._get_cached_pdf(source_md5)
                if cached_pdf and cached_meta:
                    from pdf_printer import get_pdf_info
                    info = get_pdf_info(cached_pdf)
                    page_count = info.get("page_count", 0)
                    if page_count > 0 and not _canceled():
                        self.status_message.emit(
                            f"📦 缓存命中(云端MD5): {file_name} → {page_count} 页，跳过下载")
                        self._report_page_count(file_id, file_name, page_count, info.get("orientation", ""))
                        return

            # 1. 下载文件到临时路径（每块检查取消标记）
            self.status_message.emit(f"☁ 页数分析: 下载 {file_name} ...")
            temp_dl = os.path.join(tempfile.gettempdir(), f"hn_analyze_{file_id}{ext}")
            resp = http_requests.get(download_url, timeout=120, stream=True)
            resp.raise_for_status()
            with open(temp_dl, "wb") as wf:
                for chunk in resp.iter_content(chunk_size=65536):
                    if _canceled():
                        self.status_message.emit(f"⛔ 页数分析已取消(下载中止): {file_name}")
                        return
                    wf.write(chunk)

            # 2. 计算源文件 MD5（后端未带 MD5 时的兜底），查缓存
            if not source_md5:
                source_md5 = self._compute_md5_file(temp_dl)
            cached_pdf, cached_meta = self._get_cached_pdf(source_md5)

            if cached_pdf and cached_meta:
                # 缓存命中：直接统计页数
                self.status_message.emit(f"📦 缓存命中: {file_name} → {cached_meta.get('page_count', '?')} 页")
                from pdf_printer import get_pdf_info
                info = get_pdf_info(cached_pdf)
                page_count = info.get("page_count", 0)
                orientation = info.get("orientation", "")
                if page_count > 0 and not _canceled():
                    self._report_page_count(file_id, file_name, page_count, orientation)
                    return

            # 3. 确定是否需要转换（优先 Word/WPS COM，降级 LibreOffice 子进程）
            if ext in (".doc", ".docx"):
                if _canceled():
                    return
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
                    if page_count > 0 and not _canceled():
                        self._report_page_count(file_id, file_name, page_count, info.get("orientation", ""))
                    elif _canceled():
                        self.status_message.emit(f"⛔ 页数分析已取消: {file_name}")
                    else:
                        self.status_message.emit(f"☁ 页数分析失败: 转换后无法读取 {file_name} 页数")
                else:
                    self.status_message.emit(f"☁ 页数分析失败: 无法转换 {file_name}")
            elif ext == ".pdf":
                from pdf_printer import get_pdf_info
                info = get_pdf_info(temp_dl)
                page_count = info.get("page_count", 0)
                if page_count > 0 and not _canceled():
                    self._report_page_count(file_id, file_name, page_count, info.get("orientation", ""))
                else:
                    self.status_message.emit(f"☁ 页数分析失败: 无法读取 {file_name} 页数")
            else:
                if not _canceled():
                    self._report_page_count(file_id, file_name, 1, "")

        except Exception as e:
            if _canceled():
                self.status_message.emit(f"⛔ 页数分析已取消: {file_name}")
            else:
                self.status_message.emit(f"☁ 页数分析失败 ({file_name}): {e}")
                logger.warning(f"页数分析失败 ({file_name}): {e}")

        finally:
            for p in (temp_dl, temp_pdf):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            self._analysis_abort.pop(file_id, None)   # 分析结束清除取消标记

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
