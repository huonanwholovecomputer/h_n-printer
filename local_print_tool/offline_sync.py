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
from datetime import datetime, timedelta

import requests as http_requests

from printer_config import DEFAULT_OWNER_NAME

logger = logging.getLogger(__name__)

MAX_RETRY_COUNT = 5  # 超过此次数的离线记录不再重试
OFFLINE_QUEUE_MAX = 5000  # 离线队列上限，超过时丢弃最旧记录


class OfflineSync:
    """管理离线订单的本地存储与自动同步。"""

    def __init__(self, db_path: str = ""):
        if not db_path:
            import paths as _paths
            db_path = _paths.local_db_path()
        self.db_path = db_path
        self._lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._sync_cycles = 0  # 同步调用计数，每 100 次清理一次旧记录
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
            # v24：订单归属标记（旧库补列；缺失时按迁移规则默认占位名/管理员自行打印）
            cols = [r[1] for r in conn.execute("PRAGMA table_info(offline_orders)").fetchall()]
            if "owner_name" not in cols:
                conn.execute(f"ALTER TABLE offline_orders ADD COLUMN owner_name TEXT DEFAULT '{DEFAULT_OWNER_NAME}'")
            if "is_admin_print" not in cols:
                conn.execute("ALTER TABLE offline_orders ADD COLUMN is_admin_print INTEGER DEFAULT 1")
            if "remark" not in cols:
                conn.execute("ALTER TABLE offline_orders ADD COLUMN remark TEXT DEFAULT ''")
            conn.commit()
            conn.close()

    # ── 保存离线订单 ──

    def save_order_offline(
        self,
        order_number: str,
        files_data: list[dict],
        total_price: float,
        created_at: str,
        owner_name: str = "",
        is_admin_print: bool = True,
        remark: str = "",
    ) -> str:
        """将订单保存到本地数据库。返回保存的临时订单号。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT INTO offline_orders (order_number, files_json, total_price, created_at,
                                               owner_name, is_admin_print, remark, synced)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (order_number, json.dumps(files_data, ensure_ascii=False), total_price,
                 created_at, owner_name or DEFAULT_OWNER_NAME, 1 if is_admin_print else 0,
                 str(remark or "")[:100]),
            )
            # P2: 队列无上限保护 —— 超过 OFFLINE_QUEUE_MAX 条时丢弃最旧记录
            cur = conn.execute(
                "DELETE FROM offline_orders WHERE id NOT IN "
                "(SELECT id FROM offline_orders ORDER BY id DESC LIMIT ?)",
                (OFFLINE_QUEUE_MAX,),
            )
            if cur.rowcount > 0:
                logger.warning(
                    f"[OFFLINE] 离线队列超过 {OFFLINE_QUEUE_MAX} 条，已丢弃最旧 {cur.rowcount} 条")
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
        owner_name: str = DEFAULT_OWNER_NAME,
        is_admin_print: bool = True,
        remark: str = "",
    ) -> bool:
        """尝试上传单个订单到服务器。返回 True 表示成功。"""
        try:
            url = f"{server_url}/api/local_orders"
            payload = {
                "order_number": order_number,
                "files": json.loads(files_json),
                "total_price": total_price,
                "created_at": created_at,
                # 订单归属（v24）
                "owner_name": owner_name or DEFAULT_OWNER_NAME,
                "is_admin_print": bool(is_admin_print),
                # 备注（离线打印的订单重连后补报，不丢）
                "remark": str(remark or "")[:100],
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
            # 跳过待换号的本地临时订单号（含 "-L" 或 "LOCAL-" 前缀）：
            # 这类订单由 gui._sync_local_orders_to_cloud 在连线时统一换正式号并上报，
            # 若在此处直接上传会污染云端订单号（HN…-L / LOCAL-* 残留），并可能产生重复单。
            rows = list(
                conn.execute(
                    """SELECT id, order_number, files_json, total_price, created_at,
                              owner_name, is_admin_print, remark
                       FROM offline_orders
                       WHERE synced = 0 AND retry_count < ?
                         AND order_number NOT LIKE '%-L%'
                         AND order_number NOT LIKE 'LOCAL-%'""",
                    (MAX_RETRY_COUNT,),
                ).fetchall()
            )
            conn.close()

        if not rows:
            return 0

        logger.info(f"[SYNC] 检测到 {len(rows)} 个待同步离线任务...")
        synced_count = 0

        for db_id, order_number, files_json, total_price, created_at, owner_name, is_admin_print, remark in rows:
            success = self.upload_order(
                db_id, order_number, files_json,
                total_price, created_at, server_url, token,
                owner_name or DEFAULT_OWNER_NAME, bool(is_admin_print), remark,
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
        # P2: 定期清理 synced=1 且创建超过 30 天的旧行（每 100 次调用清理一次）
        self._sync_cycles += 1
        if self._sync_cycles >= 100:
            self._sync_cycles = 0
            self._cleanup_old_synced_rows()
        return synced_count

    def _cleanup_old_synced_rows(self):
        """删除 synced=1 且创建时间超过 30 天的旧行，避免数据库无限增长。"""
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.execute(
                    "DELETE FROM offline_orders WHERE synced = 1 AND created_at < ?",
                    (cutoff,),
                )
                conn.commit()
                n = cur.rowcount
                conn.close()
            if n > 0:
                logger.info(f"[SYNC] 已清理 {n} 条超过 30 天的已同步记录")
        except Exception as e:
            logger.warning(f"[SYNC] 清理旧同步记录失败: {e}")

    # ── 后台定时同步 ──

    def start_background_sync(self, server_url: str, token: str, interval: int = 60):
        """启动后台定时同步线程（守护线程，主程序退出时自动终止）。"""
        self._stop_event.clear()
        if self._sync_thread and self._sync_thread.is_alive():
            logger.warning("[SYNC] 后台同步已在运行，忽略重复 start_background_sync 调用")
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

    def count_unsynced(self) -> int:
        """返回所有未同步记录数（含重试耗尽的行；清空本地库前的安全校验用）。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT COUNT(*) FROM offline_orders WHERE synced = 0",
            ).fetchone()
            conn.close()
            return row[0] if row else 0

    def list_pending_orders(self, like: str = "") -> list:
        """列出待同步的离线订单行（可按 order_number LIKE 过滤），供换号流程使用。
        返回行元组: (id, order_number, files_json, total_price, created_at, owner_name, is_admin_print, remark)。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            if like:
                rows = list(conn.execute(
                    """SELECT id, order_number, files_json, total_price, created_at,
                              owner_name, is_admin_print, remark
                       FROM offline_orders
                       WHERE synced = 0 AND retry_count < ? AND order_number LIKE ?""",
                    (MAX_RETRY_COUNT, like),
                ).fetchall())
            else:
                rows = list(conn.execute(
                    """SELECT id, order_number, files_json, total_price, created_at,
                              owner_name, is_admin_print, remark
                       FROM offline_orders
                       WHERE synced = 0 AND retry_count < ?""",
                    (MAX_RETRY_COUNT,),
                ).fetchall())
            conn.close()
            return rows

    def find_pending_by_number(self, order_number: str):
        """按订单号查找待同步行（换号后用于孤儿单上报）。返回行元组或 None。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT id, order_number, files_json, total_price, created_at,
                          owner_name, is_admin_print, remark
                   FROM offline_orders WHERE order_number = ? AND synced = 0""",
                (order_number,),
            ).fetchone()
            conn.close()
        return row

    def rename_order_number(self, old_number: str, new_number: str) -> int:
        """换号流程：把离线库中旧号的待同步行改为新号（保持待同步，随后统一上报）。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute(
                "UPDATE offline_orders SET order_number = ? WHERE order_number = ? AND synced = 0",
                (new_number, old_number),
            )
            conn.commit()
            n = cur.rowcount
            conn.close()
        return n

    def mark_synced(self, order_number: str) -> None:
        """把指定订单号的待同步行标记为已同步（换号流程上报成功后调用）。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE offline_orders SET synced = 1 WHERE order_number = ? AND synced = 0",
                (order_number,),
            )
            conn.commit()
            conn.close()

    def clear_all(self) -> int:
        """清空本地订单库全部记录（关闭云端 / 订单已全部上云时调用）。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute("DELETE FROM offline_orders")
            conn.commit()
            n = cur.rowcount
            conn.close()
        if n > 0:
            logger.info(f"[OFFLINE] 已清空本地订单库 {n} 条记录")
        return n

    def clear_synced(self) -> int:
        """删除已同步（synced=1）的记录（订单已上云，本地不再留存副本）。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute("DELETE FROM offline_orders WHERE synced = 1")
            conn.commit()
            n = cur.rowcount
            conn.close()
        if n > 0:
            logger.info(f"[OFFLINE] 已清理 {n} 条已同步记录")
        return n

    @staticmethod
    def generate_local_order_number() -> str:
        """生成本地临时订单号（离线时使用）。格式: LOCAL-YYYYMMDD-序号（当天自增，便于阅读核对）。

        已废弃（v4.2）：离线订单号统一为 gui 的 HN{date}-L{seq}（连线后自动换正式号），
        此函数仅保留给遗留数据的读取/兼容，不再由新代码调用。"""
        day = datetime.now().strftime("%Y%m%d")
        prefix = f"LOCAL-{day}-"
        # 读取当天前缀下最大的「纯数字序号」——旧版 uuid 十六进制串（如 a1b2c3d4）不参与计数，
        # 避免字符串排序被非数字串干扰导致序号回退/重复。
        seq = 0
        try:
            import paths as _paths
            db = _paths.local_db_path()
            conn = sqlite3.connect(db)
            rows = conn.execute(
                "SELECT order_number FROM offline_orders WHERE order_number LIKE ?",
                (prefix + "%",),
            ).fetchall()
            conn.close()
            for (num,) in rows or []:
                tail = str(num).rsplit("-", 1)[-1]
                if tail.isdigit():
                    seq = max(seq, int(tail))
        except Exception:
            seq = 0
        return f"{prefix}{seq + 1:05d}"
