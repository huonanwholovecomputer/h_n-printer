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


def _auth_headers(token: str) -> dict:
    """S1-01：printer token 走 `X-Printer-Token` 头（不再拼 URL query）。

    与 `cloud_client.auth_headers()` 同一口径；本模块故意不 import cloud_client
    （那会连带引入 PySide6，离线库层面保持无 Qt 依赖）。
    """
    return {"X-Printer-Token": token} if token else {}

# S1-09（客户端韧性）：**离线单永不放弃**。
# 旧行为：retry_count 到 5 次就再也不会被扫到（SYNC 查询里 `retry_count < 5`），离线单
# 静默变成"永远不会上云"的僵尸行 —— 门店断网半小时、或云端临时 502 五次，订单就永久丢了。
# 现在：不按次数放弃（`MAX_RETRY_COUNT` 只作为"重试次数里程碑"用于日志/展示），单条失败
# 走**指数退避**（60s × 1.5^n，上限 1 小时）后继续重试，直到成功或云端明确业务拒收
# （4xx：重试也不会成功 ⇒ status='rejected'，仍然可见、可人工重试，绝不删行）。
MAX_RETRY_COUNT = 50          # 兼容既有引用；不是"放弃阈值"（不再用于 WHERE 过滤）
NEVER_GIVE_UP = True          # 语义常量：离线队列不因重试次数丢单
RETRY_BACKOFF_FIRST_S = 60    # 第 1 次失败后的等待时间
RETRY_BACKOFF_FACTOR = 1.5    # 指数退避倍数
RETRY_BACKOFF_MAX_S = 3600    # 单条退避上限（1 小时）
SYNC_LOOP_BACKOFF_MAX_S = 300  # 后台同步循环自身退避上限（5 分钟）

OFFLINE_QUEUE_MAX = 5000  # 离线队列上限，超过时丢弃最旧记录

# offline_orders.status 取值（S1-09）
STATUS_PENDING = "pending"     # 未同步（等待重试 / 正在重试）
STATUS_SYNCED = "synced"       # 已同步
STATUS_REJECTED = "rejected"   # 云端业务拒收（4xx）—— 自动重试无意义，保留给人看/手动重试


def retry_backoff_seconds(retry_count: int) -> float:
    """第 n 次失败后要等多久（秒）：60 × 1.5^(n-1)，上限 3600。纯函数，便于验收。"""
    n = max(1, int(retry_count or 1))
    return min(RETRY_BACKOFF_FIRST_S * (RETRY_BACKOFF_FACTOR ** (n - 1)),
               RETRY_BACKOFF_MAX_S)


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
        # D5：后台同步的**当前**目标（server_url, token）。单次元组赋值在 CPython 下是
        # 原子的 ⇒ 循环线程读到的要么是整对旧值、要么是整对新值，不会撕裂成「新地址+旧 token」。
        self._sync_target: tuple[str, str] = ("", "")
        # S1-09：上一轮同步有没有遇到网络错误（后台循环据此做 1.5× 指数退避）
        self._last_cycle_network_ok = True
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
            # S1-09：状态列 + 退避调度列（旧库补列，幂等迁移）
            #   status        —— 'pending' 未同步 / 'synced' 已同步 / 'rejected' 云端业务拒收
            #   next_retry_at —— 早于该时间（epoch 秒）的行本轮不扫（指数退避）
            #   last_error    —— 最近一次失败原因（展示给人看，不静默吞掉）
            if "status" not in cols:
                conn.execute(
                    f"ALTER TABLE offline_orders ADD COLUMN status TEXT DEFAULT '{STATUS_PENDING}'")
            if "next_retry_at" not in cols:
                conn.execute("ALTER TABLE offline_orders ADD COLUMN next_retry_at REAL DEFAULT 0")
            if "last_error" not in cols:
                conn.execute("ALTER TABLE offline_orders ADD COLUMN last_error TEXT DEFAULT ''")
            # 旧库回填：synced=1 的行 status 必须是 'synced'（否则会被当成"未同步"再上报一次）
            conn.execute(
                f"UPDATE offline_orders SET status = '{STATUS_SYNCED}' "
                f"WHERE synced = 1 AND (status IS NULL OR status <> '{STATUS_SYNCED}')")
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
                                               owner_name, is_admin_print, remark, synced,
                                               status, next_retry_at, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, '')""",
                (order_number, json.dumps(files_data, ensure_ascii=False), total_price,
                 created_at, owner_name or DEFAULT_OWNER_NAME, 1 if is_admin_print else 0,
                 str(remark or "")[:100], STATUS_PENDING),
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
        ok, _rejected, _err = self._upload_order_detail(
            db_id, order_number, files_json, total_price, created_at, server_url, token,
            owner_name, is_admin_print, remark,
        )
        return ok

    def _upload_order_detail(
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
    ) -> tuple[bool, bool, str]:
        """上传单条离线订单，返回 `(成功?, 云端业务拒收?, 错误说明)`。

        **业务拒收（4xx）与网络/服务端故障要分开**（S1-09）：
          · 4xx = 云端明确说"这单我不要/不能收"（订单号非法、已被别的来源占用…）——
            无限重试没有意义，标记 status='rejected'，仍然留在库里可见、可人工重试；
          · 网络异常 / 5xx / 429 = 暂时性 —— 保留 pending，按指数退避继续重试（不放弃）。
        """
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
            # S1-09：请求超时 10s（断网时不会把后台线程挂在半开连接上）
            # S1-01：token 走请求头（旧写法 ?token= 会落进日志/代理）
            resp = http_requests.post(url, headers=_auth_headers(token), json=payload, timeout=10)
            if resp.ok and resp.json().get("success"):
                logger.info(f"[SYNC] 上传成功: {order_number}")
                return True, False, ""
            msg = ""
            try:
                msg = resp.json().get("message", resp.text[:200])
            except Exception:
                msg = resp.text[:200] if resp.text else "无响应"
            rejected = 400 <= int(getattr(resp, "status_code", 0) or 0) < 500
            logger.warning(f"[SYNC] 上传失败 ({order_number})"
                           f"{'（云端拒收，停止自动重试）' if rejected else ''}: {msg}")
            return False, rejected, str(msg or "")[:300]
        except Exception as e:
            logger.warning(f"[SYNC] 网络异常 ({order_number}): {e}")
            return False, False, str(e)[:300]

    # ── 批量同步 ──

    def sync_all_pending_orders(self, server_url: str = "", token: str = "") -> int:
        """扫描并上传所有未同步的离线订单。返回本次同步成功的数量。

        S1-09：**不按重试次数放弃**，按 `next_retry_at` 退避调度；`status='rejected'`
        （云端业务拒收）的行不参与自动重试，留给人工在「离线未同步订单」窗里手动重试。
        """
        if not server_url or not token:
            logger.debug("[SYNC] 未提供服务器 URL 或 token，跳过同步")
            return 0

        self._last_cycle_network_ok = True
        now = time.time()
        rows = []
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            # 跳过待换号的本地临时订单号（含 "-L" 或 "LOCAL-" 前缀）：
            # 这类订单由 gui._sync_local_orders_to_cloud 在连线时统一换正式号并上报，
            # 若在此处直接上传会污染云端订单号（HN…-L / LOCAL-* 残留），并可能产生重复单。
            # 注意：**没有 retry_count 上限过滤**（永不放弃），只有退避时间 + 拒收状态过滤。
            rows = list(
                conn.execute(
                    """SELECT id, order_number, files_json, total_price, created_at,
                              owner_name, is_admin_print, remark
                       FROM offline_orders
                       WHERE synced = 0
                         AND COALESCE(status, 'pending') <> ?
                         AND COALESCE(next_retry_at, 0) <= ?
                         AND order_number NOT LIKE '%-L%'
                         AND order_number NOT LIKE 'LOCAL-%'""",
                    (STATUS_REJECTED, now),
                ).fetchall()
            )
            conn.close()

        if not rows:
            return 0

        logger.info(f"[SYNC] 检测到 {len(rows)} 个待同步离线任务...")
        synced_count = 0

        for db_id, order_number, files_json, total_price, created_at, owner_name, is_admin_print, remark in rows:
            ok, rejected, err = self._upload_order_detail(
                db_id, order_number, files_json,
                total_price, created_at, server_url, token,
                owner_name or DEFAULT_OWNER_NAME, bool(is_admin_print), remark,
            )
            if not ok and not rejected:
                # 判断"是不是网络/暂时性故障"：业务拒收已单独标记，其余算暂时性
                self._last_cycle_network_ok = False

            with self._lock:
                conn = sqlite3.connect(self.db_path)
                if ok:
                    conn.execute(
                        "UPDATE offline_orders SET synced = 1, status = ?, retry_count = 0, "
                        "next_retry_at = 0, last_error = '' WHERE id = ?",
                        (STATUS_SYNCED, db_id),
                    )
                    synced_count += 1
                elif rejected:
                    conn.execute(
                        "UPDATE offline_orders SET retry_count = retry_count + 1, "
                        "next_retry_at = 0, last_error = ?, status = ? WHERE id = ?",
                        (err or "云端拒收", STATUS_REJECTED, db_id),
                    )
                else:
                    # 指数退避：按"本轮之后"的重试次数算等待时间
                    cur = conn.execute(
                        "SELECT retry_count FROM offline_orders WHERE id = ?", (db_id,)
                    ).fetchone()
                    next_retry = now + retry_backoff_seconds((cur[0] if cur else 0) + 1)
                    conn.execute(
                        "UPDATE offline_orders SET retry_count = retry_count + 1, "
                        "next_retry_at = ?, last_error = ?, status = ? WHERE id = ?",
                        (next_retry, err or "", STATUS_PENDING, db_id),
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
        """启动后台定时同步线程（守护线程，主程序退出时自动终止）。

        D5 修复：**无论线程是否已在运行，都先记下新目标**。旧写法把 server_url/token 焊进
        闭包、且「已在运行」直接 return ⇒ 用户在「云端连接设置」里改了地址/token 之后，本次
        运行内后台定时同步仍往**旧地址**上报（旧云端会收到不属于它的本地订单）。
        `_sync_target` 用单次元组赋值更新（CPython 原子），循环里每轮重新读取。
        """
        self._sync_target = (server_url or "", token or "")
        self._stop_event.clear()
        if self._sync_thread and self._sync_thread.is_alive():
            logger.info(f"[SYNC] 后台同步已在运行 → 仅更新目标（{server_url}）")
            return  # 线程继续跑，但下一轮就用新目标

        def _loop():
            # S1-09：后台同步也是"自调度循环"—— 上一轮跑完才排下一轮；失败（网络/服务端
            # 暂时性故障）按 1.5× 指数退避（上限 5 分钟），成功即恢复基线间隔。
            delay = float(interval)
            while not self._stop_event.wait(delay):
                url, tok = self._sync_target   # 每轮取当前目标，不在闭包里持有旧值
                if not url or not tok:
                    delay = float(interval)
                    continue   # 目标被清空（云端关闭）→ 不发请求，也不白耗 retry_count
                try:
                    self.sync_all_pending_orders(url, tok)
                    ok = bool(getattr(self, "_last_cycle_network_ok", True))
                except Exception as e:
                    logger.error(f"[SYNC] 后台同步异常: {e}")
                    ok = False
                if ok:
                    delay = float(interval)
                else:
                    delay = min(delay * RETRY_BACKOFF_FACTOR, float(SYNC_LOOP_BACKOFF_MAX_S))
                    logger.debug(f"[SYNC] 上一轮失败 → {delay:.0f}s 后再试")

        self._sync_thread = threading.Thread(target=_loop, daemon=True)
        self._sync_thread.start()
        logger.info(f"[SYNC] 后台同步已启动（基线间隔 {interval}s，失败退避 1.5× / 上限 "
                    f"{SYNC_LOOP_BACKOFF_MAX_S}s）")

    def stop_background_sync(self):
        """停止后台同步线程。"""
        self._stop_event.set()
        if self._sync_thread:
            self._sync_thread.join(timeout=2)
            self._sync_thread = None

    # ── 工具方法 ──

    def pending_count(self) -> int:
        """返回尚未同步的离线订单数量（S1-09：**不按重试次数过滤**，永不放弃）。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT COUNT(*) FROM offline_orders WHERE synced = 0",
            ).fetchone()
            conn.close()
            return row[0] if row else 0

    def count_unsynced(self) -> int:
        """返回所有未同步记录数（含拒收的行；清空本地库前的安全校验用）。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT COUNT(*) FROM offline_orders WHERE synced = 0",
            ).fetchone()
            conn.close()
            return row[0] if row else 0

    def status_summary(self) -> dict:
        """未同步订单的状态摘要（供 GUI 角标/离线队列表格）：{pending, rejected, unsynced}。"""
        out = {"pending": 0, "rejected": 0, "unsynced": 0}
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                rows = list(conn.execute(
                    "SELECT COALESCE(status, 'pending') AS st, COUNT(*) "
                    "FROM offline_orders WHERE synced = 0 GROUP BY st").fetchall())
                conn.close()
            for st, n in rows:
                out[st] = int(n or 0)
                out["unsynced"] += int(n or 0)
        except Exception as e:
            logger.warning(f"[SYNC] 统计离线状态失败: {e}")
        return out

    def list_unsynced_orders(self, limit: int = 200) -> list[dict]:
        """列出未同步订单（供「离线未同步订单」窗口展示 + 手动重试）。

        返回字典列表：id / order_number / created_at / total_price / retry_count /
        status / last_error / next_retry_at / file_count。
        ⚠ 这些单据**没有**在云端落库（或落库后仍是 reserved），展示时必须标「离线未同步」，
        绝不能显示成「已完成」。
        """
        out: list[dict] = []
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                rows = list(conn.execute(
                    """SELECT id, order_number, created_at, total_price, retry_count,
                              COALESCE(status, 'pending'), COALESCE(last_error, ''),
                              COALESCE(next_retry_at, 0), files_json
                       FROM offline_orders WHERE synced = 0
                       ORDER BY id DESC LIMIT ?""",
                    (int(limit or 200),),
                ).fetchall())
                conn.close()
            for (db_id, num, created, price, retries, st, err, next_at, files_json) in rows:
                try:
                    file_count = len(json.loads(files_json) or [])
                except Exception:
                    file_count = 0
                out.append({
                    "id": db_id,
                    "order_number": num,
                    "created_at": created,
                    "total_price": float(price or 0),
                    "retry_count": int(retries or 0),
                    "status": st or STATUS_PENDING,
                    "last_error": err or "",
                    "next_retry_at": float(next_at or 0),
                    "file_count": file_count,
                })
        except Exception as e:
            logger.warning(f"[SYNC] 列出未同步订单失败: {e}")
        return out

    def retry_now(self, order_number: str = "") -> int:
        """手动重试：清掉退避/拒收标记，让下一轮同步立刻（或本轮手动调用）重试。

        参数为空 = 全部未同步订单。返回被重置的行数。
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            if order_number:
                cur = conn.execute(
                    "UPDATE offline_orders SET retry_count = 0, next_retry_at = 0, "
                    "status = ?, last_error = '' WHERE synced = 0 AND order_number = ?",
                    (STATUS_PENDING, order_number),
                )
            else:
                cur = conn.execute(
                    "UPDATE offline_orders SET retry_count = 0, next_retry_at = 0, "
                    "status = ?, last_error = '' WHERE synced = 0",
                    (STATUS_PENDING,),
                )
            conn.commit()
            n = cur.rowcount
            conn.close()
        if n > 0:
            logger.info(f"[SYNC] 已重置 {n} 条离线订单的重试状态（手动重试）")
        return n

    def list_pending_orders(self, like: str = "") -> list:
        """列出待同步的离线订单行（可按 order_number LIKE 过滤），供换号流程使用。
        返回行元组: (id, order_number, files_json, total_price, created_at, owner_name, is_admin_print, remark)。
        S1-09：**不按 retry_count 过滤** —— 换号流程要能捞到"已经重试过很多次"的单。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            if like:
                rows = list(conn.execute(
                    """SELECT id, order_number, files_json, total_price, created_at,
                              owner_name, is_admin_print, remark
                       FROM offline_orders
                       WHERE synced = 0 AND order_number LIKE ?""",
                    (like,),
                ).fetchall())
            else:
                rows = list(conn.execute(
                    """SELECT id, order_number, files_json, total_price, created_at,
                              owner_name, is_admin_print, remark
                       FROM offline_orders
                       WHERE synced = 0""",
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
                "UPDATE offline_orders SET synced = 1, status = ?, next_retry_at = 0, "
                "last_error = '' WHERE order_number = ? AND synced = 0",
                (STATUS_SYNCED, order_number),
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
