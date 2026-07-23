"""
offline_sync.py — 离线订单同步模块
当本地打印工具离线时，将订单暂存到本地 SQLite 数据库，
联网后自动上传到后端，保证打印记录不丢失。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime

import requests as http_requests

logger = logging.getLogger(__name__)

MAX_RETRY_COUNT = 5  # 超过此次数的离线记录不再重试


class OfflineSync:
    """管理离线订单的本地存储与自动同步。"""

    def __init__(self, db_path: str = ""):
        if not db_path:
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "printer-local.db")
        self.db_path = db_path
        self._lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._init_db()

    # ── 数据库初始化 ──

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS offline_orders (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_number TEXT    NOT NULL,
                    files_json   TEXT    NOT NULL,
                    total_price  REAL    DEFAULT 0,
                    created_at   TEXT    NOT NULL,
                    synced       INTEGER DEFAULT 0,
                    retry_count  INTEGER DEFAULT 0
                )
            """)
            conn.commit()
            conn.close()

    # ── 保存离线订单 ──

    def save_order_offline(
        self,
        order_number: str,
        files_data: list[dict],
        total_price: float,
        created_at: str,
    ) -> str:
        """将订单保存到本地数据库。返回保存的临时订单号。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT INTO offline_orders (order_number, files_json, total_price, created_at, synced)
                   VALUES (?, ?, ?, ?, 0)""",
                (order_number, json.dumps(files_data, ensure_ascii=False), total_price, created_at),
            )
            conn.commit()
            conn.close()
        logger.info(f"[OFFLINE] 任务已缓存: {order_number} ({len(files_data)} 个文件)")
        return order_number

    # ── 上传单个订单 ──

    def upload_order(
        self,
        db_id: int,
        order_number: str,
        files_json: str,
        total_price: float,
        created_at: str,
        server_url: str,
        token: str,
    ) -> bool:
        """尝试上传单个订单到服务器。返回 True 表示成功。"""
        try:
            url = f"{server_url}/api/local_orders"
            payload = {
                "order_number": order_number,
                "files": json.loads(files_json),
                "total_price": total_price,
                "created_at": created_at,
            }
            resp = http_requests.post(url, params={"token": token}, json=payload, timeout=10)
            if resp.ok and resp.json().get("success"):
                logger.info(f"[SYNC] 上传成功: {order_number}")
                return True
            else:
                msg = ""
                try:
                    msg = resp.json().get("message", resp.text[:200])
                except Exception:
                    msg = resp.text[:200] if resp.text else "无响应"
                logger.warning(f"[SYNC] 上传失败 ({order_number}): {msg}")
                return False
        except Exception as e:
            logger.warning(f"[SYNC] 网络异常 ({order_number}): {e}")
            return False

    # ── 批量同步 ──

    def sync_all_pending_orders(self, server_url: str = "", token: str = "") -> int:
        """扫描并上传所有未同步的离线订单。返回本次同步成功的数量。"""
        if not server_url or not token:
            logger.debug("[SYNC] 未提供服务器 URL 或 token，跳过同步")
            return 0

        rows = []
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            rows = list(
                conn.execute(
                    """SELECT id, order_number, files_json, total_price, created_at
                       FROM offline_orders
                       WHERE synced = 0 AND retry_count < ?""",
                    (MAX_RETRY_COUNT,),
                ).fetchall()
            )
            conn.close()

        if not rows:
            return 0

        logger.info(f"[SYNC] 检测到 {len(rows)} 个待同步离线任务...")
        synced_count = 0

        for db_id, order_number, files_json, total_price, created_at in rows:
            success = self.upload_order(
                db_id, order_number, files_json,
                total_price, created_at, server_url, token,
            )

            with self._lock:
                conn = sqlite3.connect(self.db_path)
                if success:
                    conn.execute("UPDATE offline_orders SET synced = 1 WHERE id = ?", (db_id,))
                    synced_count += 1
                else:
                    conn.execute(
                        "UPDATE offline_orders SET retry_count = retry_count + 1 WHERE id = ?",
                        (db_id,),
                    )
                conn.commit()
                conn.close()

        if synced_count > 0:
            logger.info(f"[SYNC] 本次同步成功 {synced_count}/{len(rows)} 个任务")
        return synced_count

    # ── 后台定时同步 ──

    def start_background_sync(self, server_url: str, token: str, interval: int = 60):
        """启动后台定时同步线程（守护线程，主程序退出时自动终止）。"""
        self._stop_event.clear()
        if self._sync_thread and self._sync_thread.is_alive():
            return  # 已在运行

        def _loop():
            while not self._stop_event.wait(interval):
                try:
                    self.sync_all_pending_orders(server_url, token)
                except Exception as e:
                    logger.error(f"[SYNC] 后台同步异常: {e}")

        self._sync_thread = threading.Thread(target=_loop, daemon=True)
        self._sync_thread.start()
        logger.info(f"[SYNC] 后台同步已启动（间隔 {interval}s）")

    def stop_background_sync(self):
        """停止后台同步线程。"""
        self._stop_event.set()
        if self._sync_thread:
            self._sync_thread.join(timeout=2)
            self._sync_thread = None

    # ── 工具方法 ──

    def pending_count(self) -> int:
        """返回尚未同步的离线订单数量。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT COUNT(*) FROM offline_orders WHERE synced = 0 AND retry_count < ?",
                (MAX_RETRY_COUNT,),
            ).fetchone()
            conn.close()
            return row[0] if row else 0

    @staticmethod
    def generate_local_order_number() -> str:
        """生成本地临时订单号（离线时使用）。格式: LOCAL-YYYYMMDD-XXXXXXXX"""
        return f"LOCAL-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
