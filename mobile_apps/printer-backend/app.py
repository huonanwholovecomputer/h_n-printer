import hashlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import string
import secrets
import sys
import threading
import time
import uuid
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from urllib import request as urlrequest, parse as urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, g, request, jsonify, send_file
from flask_socketio import SocketIO, emit, disconnect, join_room
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

try:
    from PIL import Image
    HAS_PIL = True
    # 防解压炸弹（P0-2）：单张图片像素数上限 5000 万，超限抛 DecompressionBombError
    Image.MAX_IMAGE_PIXELS = 50_000_000
except ImportError:
    HAS_PIL = False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 最大上传 50MB
# HTTP CORS：桌面浏览器预览（file:// 或 localhost）与 Capacitor WebView（https://localhost）
# 均需跨域访问 /api/*。认证走 Bearer token（非 Cookie），放行任意 Origin 无越权风险，
# 与下方 SocketIO 的 cors_allowed_origins="*" 保持一致。只配此一层即可，nginx 不要再加
# Access-Control-* 头，避免出现重复头。
# max_age=86400：浏览器缓存 OPTIONS 预检 1 天，避免每次刷新都重发预检导致 nginx 限流 503
CORS(app, resources={r"/api/*": {"origins": "*"}}, max_age=86400)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DATABASE = "orders.db"
USER_DATABASE = "users.db"
UPLOAD_DIR = "uploads"
# 注意（P0-2）：avatars/ 目录（用户头像）不参与保留期清理与缓存统计——用户数据不随
# 打印文件过期删除是设计使然，但头像目前没有数量/大小配额与独立清理机制，长期运行
# 可能持续占用磁盘（每次上传 ≤2MB，但无人回收）。如需要回收需另加独立策略（如
# 按用户头像数量上限或未活跃时间清理）。
AVATAR_DIR = os.path.join(UPLOAD_DIR, "avatars")
MD5_INDEX_FILE = os.path.join(UPLOAD_DIR, "md5_index.json")
RETENTION_CONFIG_FILE = os.path.join(UPLOAD_DIR, "retention_config.json")

# 打印机设备注册表 / 接单接管状态（2026-11 新增：多设备共连服务器时的唯一接管者机制）
# devices.json: { client_id: {device_name, owner_name, first_seen, last_seen} }
DEVICES_FILE = "devices.json"
# printer_claim.json: { active_client_id, owner_name, claimed_at } — 当前启用「接单」的设备
CLAIM_FILE = "printer_claim.json"

# 默认保留时间：7 天
DEFAULT_RETENTION = {"days": 7, "hours": 0}

# -------- 防滥用 / DDoS 防护阈值 --------
# 管理员/超管可通过 /api/admin/security 查看和调整，持久化到 uploads/security_config.json，
# 运行中保存立即生效（无需重启）。0 表示关闭对应检查。各项说明：
#   - user_quota_mb           每用户累计文件配额（MB），超限拒绝上传（防存储型 DDoS）
#   - disk_min_free_mb        磁盘剩余空间守卫（MB），低于阈值拒绝上传
#   - queued_timeout_hours    排队子任务超时淘汰（小时），释放被 queued 订单钉住的磁盘文件
#   - upload_rate_limit       每用户每分钟上传次数上限
#   - submit_order_rate_limit 每用户每分钟提交订单次数上限
#   - device_login_rate_limit 每 IP / 每设备每小时设备登录次数上限
#   - redeem_rate_limit       每用户每分钟密钥兑换次数上限
#   - log_report_rate_limit   每 IP 每分钟日志上报条数上限
SECURITY_CONFIG_FILE = os.path.join(UPLOAD_DIR, "security_config.json")

DEFAULT_SECURITY = {
    "user_quota_mb": 2048,
    "disk_min_free_mb": 5120,
    "queued_timeout_hours": 24,
    "upload_rate_limit": 20,
    "submit_order_rate_limit": 20,
    "device_login_rate_limit": 20,
    "redeem_rate_limit": 10,
    "log_report_rate_limit": 30,
}

# 每项允许范围 (min, max)，POST 保存时校验（0 是否允许见 DEFAULT_SECURITY 注释）
SECURITY_RANGES = {
    "user_quota_mb": (0, 102400),
    "disk_min_free_mb": (0, 102400),
    "queued_timeout_hours": (0, 720),
    "upload_rate_limit": (1, 600),
    "submit_order_rate_limit": (1, 600),
    "device_login_rate_limit": (1, 600),
    "redeem_rate_limit": (1, 600),
    "log_report_rate_limit": (1, 600),
}

_SECURITY = dict(DEFAULT_SECURITY)


def load_security_config():
    """加载防滥用配置（文件缺失/损坏时回退默认值），返回配置副本。"""
    global _SECURITY
    cfg = dict(DEFAULT_SECURITY)
    if os.path.exists(SECURITY_CONFIG_FILE):
        try:
            with open(SECURITY_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in cfg:
                if k in data and isinstance(data[k], (int, float)):
                    cfg[k] = int(data[k])
        except (json.JSONDecodeError, IOError):
            pass
    _SECURITY = cfg
    return dict(cfg)


def save_security_config(cfg):
    """保存防滥用配置并立即更新内存副本（运行时生效，无需重启）。"""
    global _SECURITY
    _SECURITY = dict(cfg)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(SECURITY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_SECURITY, f, ensure_ascii=False, indent=2)


# 模块加载即读取已保存的配置（若无则用默认值）
_SECURITY = load_security_config()

# 已知扩展名 → 子目录映射
EXT_DIR_MAP = {
    "pdf": "pdf", "doc": "doc", "docx": "docx", "xls": "xls", "xlsx": "xlsx",
    "ppt": "ppt", "pptx": "pptx", "txt": "txt", "csv": "csv",
    "png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif", "bmp": "bmp",
    "webp": "webp", "tiff": "tiff", "tif": "tiff", "svg": "svg",
    "zip": "zip", "rar": "rar", "7z": "7z",
}


# -------- 全局数据库锁与独立连接（供后台线程和加锁事务使用）--------
db_lock = threading.Lock()

# -------- MD5 索引文件锁（P2-5）：load/save 均需持锁，防并发读写损坏索引 --------
_md5_index_lock = threading.Lock()

# -------- 内存限速器（P1-6）--------
# 滑动窗口 {key: [timestamp, ...]}。注意：内存计数仅对单 worker 生效，
# 生产环境多 worker / 多实例时需要在 nginx 层再加 IP 维度限速。
_rate_limits = defaultdict(list)
_rate_limits_lock = threading.Lock()

# 页数分析惰性触发防抖：file_id → 最近一次主动推送时间戳
# （前端 /api/file_page 轮询时，若打印机在线且文件未分析，可主动补推；
#   30s 防抖避免每 2s 轮询刷爆分析请求，也避免补推 + 轮询双重推送）
_last_page_analysis_push = {}
_last_page_analysis_push_lock = threading.Lock()
# 页数分析推送目标 sid 记录（file_id → sid），供取消分析时精确转发
_analysis_push_sid = {}


def _rate_limit(key, max_count, window_seconds):
    """滑动窗口限速：key 在 window_seconds 秒内最多 max_count 次，超限返回 False。"""
    now = time.time()
    with _rate_limits_lock:
        ts_list = _rate_limits[key]
        # 清理窗口外的旧时间戳（顺序追加，队头最旧）
        while ts_list and ts_list[0] <= now - window_seconds:
            ts_list.pop(0)
        if len(ts_list) >= max_count:
            return False
        ts_list.append(now)
        return True


class OrderRejected(Exception):
    """提交订单业务校验失败（返回 400）：在事务内抛出以中止并回滚。"""
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def get_db_conn():
    """独立的数据库连接，不依赖 Flask 应用上下文，专供后台线程和加锁事务使用"""
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_ext_dir(ext):
    """根据扩展名返回对应的子目录名，若未知则返回 'other'"""
    ext = ext.lower().lstrip(".")
    return EXT_DIR_MAP.get(ext, "other")


def get_file_md5(file_path):
    """分块计算文件 MD5，支持大文件"""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def load_md5_index():
    """加载 MD5 索引文件，不存在则返回空字典。
    兼容两种格式：旧 {md5: rel_path} 和 新 {md5: {path, original_name, page_count, page_count_verified}}"""
    with _md5_index_lock:
        if not os.path.exists(MD5_INDEX_FILE):
            return {}
        try:
            with open(MD5_INDEX_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}


def save_md5_index(index):
    """保存 MD5 索引到文件"""
    with _md5_index_lock:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        with open(MD5_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)


def _md5_entry_get(index, md5):
    """读取 MD5 条目，兼容旧格式 {md5: rel_path}"""
    val = index.get(md5)
    if val is None:
        return None
    if isinstance(val, str):
        return {"path": val, "original_name": "", "page_count": 0, "page_count_verified": False}
    return val


def _md5_entry_set(index, md5, path=None, original_name=None, page_count=None, page_count_verified=None):
    """写入/更新 MD5 条目，自动升级旧格式。返回更新后的 index。"""
    existing = index.get(md5)
    if isinstance(existing, str):
        existing = {"path": existing, "original_name": "", "page_count": 0, "page_count_verified": False}
    elif existing is None:
        existing = {}
    if path is not None:
        existing["path"] = path
    if original_name is not None:
        existing["original_name"] = original_name
    if page_count is not None:
        existing["page_count"] = page_count
    if page_count_verified is not None:
        existing["page_count_verified"] = page_count_verified
    index[md5] = existing
    return index


def _md5_entry_has_verified_count(index, md5):
    """检查某 MD5 是否有已验证的页数"""
    entry = _md5_entry_get(index, md5)
    if not entry:
        return False
    return entry.get("page_count", 0) > 0 and entry.get("page_count_verified", False)


def build_md5_index():
    """扫描 uploads/ 目录下所有文件，构建 MD5 索引。已有索引则跳过全量重建但会补全缺失条目。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # 提取所有已索引的相对路径（兼容新旧格式）
    def _indexed_paths(idx):
        paths = set()
        for v in idx.values():
            if isinstance(v, str):
                paths.add(v)
            elif isinstance(v, dict) and v.get("path"):
                paths.add(v["path"])
        return paths

    existing = load_md5_index()
    if existing:
        indexed = _indexed_paths(existing)
        for root, dirs, files in os.walk(UPLOAD_DIR):
            if os.path.basename(root) == "avatars":
                continue
            for fname in files:
                if fname == "md5_index.json":
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, UPLOAD_DIR)
                if rel not in indexed:
                    try:
                        md5 = get_file_md5(fpath)
                        if md5 not in existing:
                            # 新格式：存储字典
                            ext = os.path.splitext(fname)[1].lower().lstrip(".")
                            existing[md5] = {
                                "path": rel, "original_name": fname,
                                "page_count": 0, "page_count_verified": False,
                            }
                            print(f"  [MD5] 补充索引: {md5[:8]}... → {rel}")
                    except Exception as e:
                        print(f"  [MD5] 扫描文件失败 {fpath}: {e}")
        save_md5_index(existing)
        print(f"  [MD5] 索引已更新，共 {len(existing)} 条记录")
        return

    # 首次构建：全量扫描，使用新格式
    print("  [MD5] 首次构建 MD5 索引...")
    index = {}
    for root, dirs, files in os.walk(UPLOAD_DIR):
        if os.path.basename(root) == "avatars":
            continue
        for fname in files:
            if fname == "md5_index.json":
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, UPLOAD_DIR)
            try:
                md5 = get_file_md5(fpath)
                ext = os.path.splitext(fname)[1].lower().lstrip(".")
                if md5 in index:
                    print(f"  [MD5] 重复文件: {fpath}")
                index[md5] = {
                    "path": rel, "original_name": fname,
                    "page_count": 0, "page_count_verified": False,
                }
            except Exception as e:
                print(f"  [MD5] 扫描文件失败 {fpath}: {e}")
    save_md5_index(index)
    print(f"  [MD5] 索引构建完成，共 {len(index)} 条记录")


def load_retention_config():
    """加载保留时间配置，不存在则返回默认值"""
    if not os.path.exists(RETENTION_CONFIG_FILE):
        return dict(DEFAULT_RETENTION)
    try:
        with open(RETENTION_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 确保必要字段存在
        cfg.setdefault("days", DEFAULT_RETENTION["days"])
        cfg.setdefault("hours", DEFAULT_RETENTION["hours"])
        return cfg
    except (json.JSONDecodeError, IOError):
        return dict(DEFAULT_RETENTION)


def save_retention_config(cfg):
    """保存保留时间配置到文件"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(RETENTION_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def cleanup_expired_files():
    """清理过期文件（删磁盘 + 清空路径）与幽灵文件记录。

    - 过期文件：删磁盘文件、清空 files.path（保留记录，历史/引用完整性）
    - 幽灵记录（path 已清空或物理文件缺失 + 未关联活跃订单）：直接删除 files 记录，
      否则会污染页数分析补推——每次打印机上线重推已清理文件 → 下载 404 刷屏

    跳过仍在活跃订单中引用的文件（reserved/queued/printing/accepted/scheduled/
    waiting/downloading），防止清理掉尚未完成或崩溃后待恢复的打印任务所需文件。
    幽灵清理不受保留期限制（物理已清理的记录无需等待过期）。"""
    cfg = load_retention_config()
    days = cfg.get("days", 0)
    hours = cfg.get("hours", 0)
    no_retention = (days == 0 and hours == 0)

    conn = get_db()

    # 收集所有活跃订单引用的 file_id
    # （P2-3）补全遗漏状态：scheduled（预约待下发）/ waiting（文件就绪等打印）/
    # downloading（文件下载中）——之前漏掉会误删预约单尚未打印所需的文件
    active_statuses = ('queued', 'printing', 'accepted', 'reserved',
                       'scheduled', 'waiting', 'downloading')
    status_placeholders = ",".join("?" for _ in active_statuses)
    active_file_ids = set()
    for (fid,) in conn.execute(
        f"SELECT DISTINCT file_id FROM orders WHERE file_id IS NOT NULL AND status IN ({status_placeholders})",
        active_statuses,
    ).fetchall():
        if fid:
            active_file_ids.add(fid)
    for (fid,) in conn.execute(
        f"SELECT DISTINCT file_id FROM order_files WHERE file_id IS NOT NULL AND status IN ({status_placeholders})",
        active_statuses,
    ).fetchall():
        if fid:
            active_file_ids.add(fid)

    if active_file_ids:
        print(f"  [CLEANUP] 跳过 {len(active_file_ids)} 个活跃订单引用的文件")

    # v4.4 修复：MD5 去重复用会让多个 files 行共享同一物理文件（上传时复用 existing_path）。
    # 收集活跃行引用的全部物理路径——即使某行已过期且非活跃，只要其物理路径仍被其他活跃行
    # 引用，删除时必须跳过；否则排队/打印中订单的文件会被提前删掉（下载 404，如订单 failed）。
    active_paths = set()
    if active_file_ids:
        _ph = ",".join("?" for _ in active_file_ids)
        for (_p,) in conn.execute(
            f"SELECT path FROM files WHERE id IN ({_ph}) AND path != ''", list(active_file_ids)
        ).fetchall():
            if _p:
                active_paths.add(os.path.normpath(_p))

    # ---- 幽灵记录清理（不受保留期限制）----
    ghost_ids = []
    for (fid, fpath) in conn.execute("SELECT id, path FROM files").fetchall():
        if fid in active_file_ids:
            continue
        if not fpath or not os.path.exists(fpath):
            ghost_ids.append(fid)
    if ghost_ids:
        placeholders = ",".join("?" for _ in ghost_ids)
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", ghost_ids)
        conn.commit()
        print(f"  [CLEANUP] 已删除 {len(ghost_ids)} 条幽灵文件记录（物理已清理，防止页数分析补推刷屏）")

    # 0 天 0 小时 = 永不过期（幽灵清理已在上方完成）
    if no_retention:
        conn.close()
        return

    cutoff = datetime.now() - timedelta(days=days, hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        "SELECT id, path FROM files WHERE created_at < ? AND path != ''",
        (cutoff_str,),
    ).fetchall()

    md5_index = load_md5_index()
    deleted_count = 0
    skipped_active = 0

    for row in rows:
        file_id = row["id"]
        file_path = row["path"]

        # 跳过活跃订单引用的文件
        if file_id in active_file_ids:
            skipped_active += 1
            continue

        # v4.4：共享物理文件保护——本行过期且非活跃，但物理路径仍被其他活跃行引用 → 跳过删除
        if file_path and os.path.normpath(file_path) in active_paths:
            skipped_active += 1
            continue

        # 删除磁盘文件
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                print(f"  [CLEANUP] 删除文件失败 {file_path}: {e}")
                continue

        # 从 MD5 索引中移除（兼容新旧格式）
        rel_path = os.path.relpath(file_path, UPLOAD_DIR) if file_path else None
        if rel_path:
            keys_to_remove = [
                k for k, v in md5_index.items()
                if (isinstance(v, str) and v == rel_path) or
                   (isinstance(v, dict) and v.get("path") == rel_path)
            ]
            for k in keys_to_remove:
                del md5_index[k]

        # 清空 files 表中的路径（保留记录本身，下轮清理时作为幽灵删除）
        conn.execute("UPDATE files SET path = '', size = 0 WHERE id = ?", (file_id,))
        deleted_count += 1

    conn.commit()
    conn.close()

    if skipped_active > 0:
        print(f"  [CLEANUP] 已跳过 {skipped_active} 个活跃订单引用的过期文件")
    if deleted_count > 0:
        save_md5_index(md5_index)
        print(f"  [CLEANUP] 已清理 {deleted_count} 个过期文件（cutoff={cutoff_str}）")
        # 通知所有在线打印机同步清理本地缓存
        _notify_clients("storage_config_updated", {
            "retention_days": days,
            "retention_hours": hours,
        })

    # 清理 uploads/ 根目录下的孤儿临时文件（上传中断遗留，无 files 表记录）
    import re
    try:
        orphan_deleted = 0
        for fname in os.listdir(UPLOAD_DIR):
            fpath = os.path.join(UPLOAD_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            # 临时文件命名格式: <32位hex uuid>.<ext>
            if not re.match(r'^[0-9a-f]{32}\.\w+$', fname):
                continue
            # 保留至少 1 小时，防止误删正在上传中的文件
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            if (datetime.now() - mtime).total_seconds() < 3600:
                continue
            try:
                os.remove(fpath)
                orphan_deleted += 1
            except OSError:
                pass
        if orphan_deleted > 0:
            print(f"  [CLEANUP] 已清理 {orphan_deleted} 个孤儿临时文件")
    except Exception as e:
        print(f"  [CLEANUP] 清理孤儿临时文件出错: {e}")


# -------- 加载配置（内部默认值 + config.py 覆盖）--------
# 所有敏感信息使用占位符/空值作为内部默认值，
# 部署后通过 config.py 覆盖（config.py 已排除在 git 外）。
WECHAT_APPID = None
WECHAT_APPSECRET = None
SECRET_KEY = "fallback-dev-key-please-change-in-production"
PUBLIC_BASE_URL = "http://127.0.0.1:5000"
ADMIN_OPENIDS = set()
SUPER_ADMIN_OPENID = None
PRINTER_TOKEN = None
PRINTER_NAME = ""

# 订单归属默认占位名（未显式指定归属时使用通用占位，避免把真实用户名硬编码为默认值）
DEFAULT_OWNER_NAME = "张三"

try:
    import config
    WECHAT_APPID = getattr(config, "WECHAT_APPID", WECHAT_APPID)
    WECHAT_APPSECRET = getattr(config, "WECHAT_APPSECRET", WECHAT_APPSECRET)
    SECRET_KEY = getattr(config, "SECRET_KEY", SECRET_KEY)
    PUBLIC_BASE_URL = getattr(config, "PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    PRINTER_TOKEN = getattr(config, "TOKEN", PRINTER_TOKEN)
    PRINTER_NAME = getattr(config, "PRINTER_NAME", PRINTER_NAME)
    ADMIN_OPENIDS = set(getattr(config, "ADMIN_OPENIDS", []))
    SUPER_ADMIN_OPENID = getattr(config, "SUPER_ADMIN_OPENID", SUPER_ADMIN_OPENID)
    if WECHAT_APPID and WECHAT_APPSECRET and SECRET_KEY != "fallback-dev-key-please-change-in-production":
        print("已加载 config.py 微信配置")
    else:
        print("[WARN] config.py 中缺少微信配置项（WECHAT_APPID / WECHAT_APPSECRET / SECRET_KEY），登录功能不可用")
    if ADMIN_OPENIDS:
        print(f"已加载 {len(ADMIN_OPENIDS)} 个管理员 openid")
except ImportError:
    print("[INFO] 未找到 config.py，使用内部默认配置。复制 config.py.example → config.py 后可自定义。")

# 安全策略（P2-7）：生产环境禁止使用内置默认 SECRET_KEY。
# 仅开发模式（FLASK_ENV=development 或命令行带 --debug）才允许默认 key。
_DEV_MODE = os.environ.get("FLASK_ENV") == "development" or "--debug" in " ".join(sys.argv)
if SECRET_KEY == "fallback-dev-key-please-change-in-production" and not _DEV_MODE:
    raise RuntimeError(
        "SECRET_KEY 未配置：生产环境必须在 config.py 中设置 SECRET_KEY，"
        "或设置 FLASK_ENV=development 以开发模式运行"
    )


def is_admin(openid):
    """判断给定 openid 是否为管理员"""
    return openid in ADMIN_OPENIDS


def compute_role(openid):
    """计算用户角色: admin / user / guest（含 DB 中 admin 角色和临时授权）"""
    if is_admin(openid):
        return "admin"
    conn = get_user_db()
    row = conn.execute("SELECT role, temp_until FROM users WHERE openid = ?", (openid,)).fetchone()
    conn.close()
    if row:
        if row["role"] == "admin":
            return "admin"
        if row["role"] == "user":
            return "user"
        # 临时授权：temp_until 未过期视为 user
        # （P2-17）时区说明：temp_until/scheduled_at 等均为本地时间字符串（%Y-%m-%d %H:%M:%S），
        # 依赖服务器系统时区；部署时必须把服务器时区设为中国时区（Asia/Shanghai），
        # 否则预约到点/临时授权过期判定会偏移。不改存储格式（破坏性风险）。
        if row["temp_until"]:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if row["temp_until"] > now_str:
                return "user"
    return "guest"


def get_avatar_url(openid, avatar_path):
    """根据 avatar_path 生成头像 URL，用文件 mtime 做缓存破坏参数"""
    if not avatar_path or not os.path.exists(avatar_path):
        return ""
    mtime = int(os.path.getmtime(avatar_path))
    return f"{PUBLIC_BASE_URL}/api/avatar?openid={openid}&v={mtime}"


_AVATAR_MAX_SIZE = 2 * 1024 * 1024  # 头像大小上限 2MB（P0-2）


def _is_valid_avatar(stream):
    """校验头像文件魔数（P0-2）：仅允许 jpeg/png/webp/gif 头，读前 8 字节。
    读取后把流位置复位到 0，供后续 save 使用。"""
    head = stream.read(8)
    stream.seek(0)
    if not head:
        return False
    if head.startswith(b"\xff\xd8\xff"):            # JPEG
        return True
    if head.startswith(b"\x89PNG\r\n\x1a\n"):       # PNG
        return True
    if head[:4] == b"RIFF" and head[4:8] == b"WEBP":  # WebP (RIFF....WEBP)
        return True
    if head.startswith(b"GIF8"):                    # GIF87a / GIF89a
        return True
    return False


def compress_avatar(file_path, max_size=200):
    """压缩头像：缩放到 max_size×max_size 以内，转 JPEG 质量 80。
    返回压缩后的文件路径（可能不同于原路径，因为扩展名变为 .jpg）；
    无法处理（损坏/解压炸弹/非图片）时返回 None，由调用方删除已保存文件并拒绝（P0-2）。"""
    if not HAS_PIL:
        return file_path  # 没有 Pillow 就原样存储

    try:
        img = Image.open(file_path)
        img.load()  # 提前完整解码，尽早暴露损坏文件/解压炸弹
        # 统一为 RGB（去掉透明通道）
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        # 等比缩放到 max_size 以内
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        # 输出为 JPEG
        jpg_path = os.path.splitext(file_path)[0] + '.jpg'
        img.save(jpg_path, 'JPEG', quality=80, optimize=True)
        # 如果原文件与压缩文件不同，删除原文件
        if jpg_path != file_path and os.path.exists(file_path):
            os.remove(file_path)
        return jpg_path
    except Exception as e:
        print(f"[WARN] 头像压缩失败: {e}")
        return None


# Token 签名器（依赖 SECRET_KEY，必须在配置加载之后初始化）
app.config["SECRET_KEY"] = SECRET_KEY
TOKEN_MAX_AGE = 7 * 24 * 3600  # token 有效期 7 天
token_serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

# 文件下载签名器（短时效，供打印机客户端下载文件用）
download_serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

# -------- 打印机客户端状态 --------
# { client_id: {"sid": socketio_sid, "heartbeat": datetime, "connected_at": datetime} }
printer_clients = {}
printer_clients_lock = threading.Lock()

# 推送后等待反馈的任务: { order_id: {"pushed_at": datetime, "client_id": str} }
pushed_tasks = {}
pushed_tasks_lock = threading.Lock()

CLIENT_HEARTBEAT_TIMEOUT = 90    # 心跳超时秒数（超过此值视为离线）
PRINT_FEEDBACK_TIMEOUT = 180     # 打印反馈超时秒数（3 分钟，断线回滚兜底）
ACCEPT_WAIT_TIMEOUT = 600        # 推送后未被 accept 的任务（打印机端等待用户确认）：超过该时长停止计时，但不判失败（任务保持等待，由孤儿回收+重推兜底）
# 断线未知超时秒数（30 分钟）：客户端断线后任务置 offline_unknown，等待原客户端重连回报；
# 超时仍未解决 → 保守标记 failed（方案 B：不重复打印，用户可手动重发）
OFFLINE_UNKNOWN_TIMEOUT = 1800

def get_active_clients():
    """返回心跳未超时的客户端 ID 列表"""
    now = datetime.now()
    active = []
    with printer_clients_lock:
        for cid, info in list(printer_clients.items()):
            if (now - info["heartbeat"]).total_seconds() < CLIENT_HEARTBEAT_TIMEOUT:
                active.append(cid)
            else:
                del printer_clients[cid]  # 清理超时客户端
    return active


# ==================== 打印机设备注册表 + 接单接管（多设备唯一接管者） ====================
# 需求背景：多台设备可同时连接同一台服务器，但只有「启用接单」的那一台能接收订单。
#   · devices.json 持久化每台设备的计算机名 / 所有者 / 首末次在线时间（跨重启保留）；
#   · printer_claim.json 持久化当前「接单」设备（谁启用了接单），重启后端不丢失。
_devices_lock = threading.Lock()
_claim_lock = threading.Lock()


def load_devices() -> dict:
    """读取设备注册表；文件缺失/损坏返回空 dict。"""
    with _devices_lock:
        if not os.path.exists(DEVICES_FILE):
            return {}
        try:
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, IOError):
            return {}


def save_devices(data: dict) -> None:
    """原子写入设备注册表。"""
    with _devices_lock:
        tmp = DEVICES_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DEVICES_FILE)
        except OSError as e:
            print(f"  [DEV] 保存设备注册表失败: {e}")


def register_device(client_id: str, device_name: str = "") -> None:
    """登记设备上线（连接/心跳/领取时调用）：记录计算机名与在线时间。"""
    if not client_id:
        return
    devices = load_devices()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = devices.get(client_id) or {}
    entry["device_name"] = (device_name or entry.get("device_name", "") or client_id)[:64]
    entry["first_seen"] = entry.get("first_seen", now_str)
    entry["last_seen"] = now_str
    devices[client_id] = entry
    save_devices(devices)


def get_device_entry(client_id: str) -> dict:
    """读取单台设备的注册信息（不存在返回空 dict）。"""
    if not client_id:
        return {}
    return load_devices().get(client_id) or {}


def get_device_owner(client_id: str) -> str:
    """读取设备绑定的所有者姓名（未绑定返回空串）。"""
    return (get_device_entry(client_id) or {}).get("owner_name", "") or ""


def _load_claim_impl() -> dict:
    """读取接单设备集合（无锁实现，供持锁调用方在 _claim_lock 内使用）。
    新格式: {"claiming_devices": {client_id: {"claimed_at": ..., "owner_name": ...}}}
    兼容旧格式: {"active_client_id": "xxx", ...} → 读取时自动归一化为 claiming_devices。"""
    if not os.path.exists(CLAIM_FILE):
        return {}
    try:
        with open(CLAIM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}
    if not isinstance(data, dict):
        return {}
    # 旧格式（唯一接管者）→ 新格式（多设备集合）
    if "claiming_devices" not in data:
        old = data.get("active_client_id", "")
        devices = {}
        if old:
            devices[old] = {
                "claimed_at": data.get("claimed_at", ""),
                "owner_name": data.get("owner_name", ""),
            }
        return {"claiming_devices": devices}
    devices = data.get("claiming_devices")
    if not isinstance(devices, dict):
        devices = {}
    return {"claiming_devices": devices}


def load_claim() -> dict:
    """读取接单接管状态。"""
    with _claim_lock:
        return _load_claim_impl()


def _save_claim_impl(data: dict) -> None:
    """原子写入接单接管状态（无锁实现，供持锁调用方在 _claim_lock 内使用）。"""
    tmp = CLAIM_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CLAIM_FILE)
    except OSError as e:
        print(f"  [CLAIM] 保存接单状态失败: {e}")


def save_claim(data: dict) -> None:
    """原子写入接单接管状态。"""
    with _claim_lock:
        _save_claim_impl(data)


def get_claiming_devices() -> dict:
    """返回所有启用接单的设备集合 {client_id: {"claimed_at", "owner_name"}}。"""
    claim = load_claim()
    devices = claim.get("claiming_devices") or {}
    return devices


def get_claiming_device_ids() -> list:
    """返回所有启用接单的设备 client_id 列表。"""
    return list(get_claiming_devices().keys())


def is_claiming(client_id: str) -> bool:
    """指定设备是否已启用接单。"""
    return bool(client_id) and client_id in get_claiming_devices()


def get_active_printer_client():
    """返回当前「启用接单」且在线的打印机 client_id（取第一个在线接单设备）；无则 None。
    多设备接单模式下仅作兼容用途（页数分析/预约兜底等需要「任一接单设备」的场景）；
    指定设备分发请用 get_claiming_devices + is_claiming。"""
    online = set(get_active_clients())
    for cid in get_claiming_device_ids():
        if cid in online:
            return cid
    return None


def is_printer_available() -> bool:
    """是否有「启用接单」的打印机（含离线设备——离线时任务排队等其上线，提交仍被接受）。"""
    return len(get_claiming_device_ids()) > 0


def device_display_label(client_id: str) -> str:
    """设备展示名：「{所有者}的设备 | {client_id}」，如「姚懿祥的设备 | DESKTOP-EJGEB1V-a6c1a1365e」。
    未绑定所有者 → 直接显示 client_id。"""
    if not client_id:
        return ""
    entry = get_device_entry(client_id)
    owner = entry.get("owner_name", "") or ""
    if owner:
        return f"{owner}的设备 | {client_id}"
    return client_id


def _printer_state_payload(client_id: str) -> dict:
    """构造发给某设备的接单状态 payload（本机是否启用接单、全部接单设备、本机所有者）。
    2026-12：多设备接单 —— claiming_devices 为全部启用接单设备，is_active 表示本机是否在其中。"""
    claiming = get_claiming_devices()
    mine = get_device_entry(client_id)
    return {
        "claiming_devices": {cid: (entry.get("owner_name", "") or "") for cid, entry in claiming.items()},
        "is_active": client_id in claiming,
        # 本机设备信息：owner_name = 本机绑定的所有者（授权页绑定），新建标签页默认归属者用
        "owner_name": mine.get("owner_name", "") or "",
        "device_name": mine.get("device_name", "") or client_id,
        # 兼容旧字段（旧版本地工具读取 active_client_id 判断「唯一接管者」）
        "active_client_id": client_id if client_id in claiming else "",
        "active_owner_name": (claiming.get(client_id) or {}).get("owner_name", ""),
        "active_device_name": mine.get("device_name", "") if client_id in claiming else "",
    }


def broadcast_printer_state():
    """向所有在线客户端推送当前接单状态（接管变化实时同步，设备端据此刷新 UI）。"""
    active = get_active_clients()
    if not active:
        return
    for cid in active:
        with printer_clients_lock:
            info = printer_clients.get(cid)
            sid = info["sid"] if info else None
        if not sid:
            continue
        try:
            socketio.emit("printer_state", _printer_state_payload(cid), to=sid)
        except Exception as e:
            print(f"  [CLAIM] 推送接单状态到 {cid} 失败: {e}")


def make_download_url(file_id):
    """生成带签名的文件下载 URL（1 小时有效）"""
    token = download_serializer.dumps(file_id)
    return f"{PUBLIC_BASE_URL}/api/download/{file_id}?t={token}"


def _iso_to_ts(s):
    """"%Y-%m-%d %H:%M:%S" → epoch 秒；解析失败返回 0"""
    try:
        return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return 0


def _get_printer_token():
    """获取打印机认证 token（P1-3）：优先 X-Printer-Token 请求头，回退 query 参数 ?token=（向后兼容）。
    安全说明：
    - query 传参会出现在 nginx 访问日志中，长期暴露有泄露风险，应优先走请求头；
    - PRINTER_TOKEN 应定期轮换（修改 config.py 中 TOKEN 后重启后端生效）。"""
    tok = request.headers.get("X-Printer-Token", "")
    if not tok:
        tok = request.args.get("token", "")
    return tok


def _get_pricing_config():
    """读取 pricing.json（不缓存，供提交订单时按服务端配置重算附加费），失败返回空 dict。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_bound_owner_name(openid: str) -> str:
    """反查 openid 在收支清算成员绑定表中绑定的成员名（2026-12 修复）。
    顾客订单推送时附带该名字，本地打印工具据此把标签页归属设为下单用户绑定的成员，
    而不是回退到「第一个成员」。无绑定 / 绑定成员已删除 / 未配置返回空串。"""
    if not openid:
        return ""
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT data FROM finance_config WHERE id = 1").fetchone()
        finally:
            conn.close()
        if not row or not row["data"]:
            return ""
        cfg = json.loads(row["data"])
        cc = cfg.get("config") or {}
        members = {}
        for m in (cc.get("members") or []):
            if isinstance(m, dict) and m.get("id"):
                members[str(m["id"])] = str(m.get("name", "")).strip()
        for b in (cc.get("memberBindings") or []):
            if isinstance(b, dict) and b.get("openid") == openid and b.get("memberId"):
                return members.get(str(b["memberId"]), "") or ""
        return ""
    except Exception as e:
        print(f"  [BIND] 反查绑定成员失败: {e}")
        return ""


# 多打印机负载均衡（P2-12）：轮询选择客户端，避免总推给 active_clients[0] 造成负载不均
_client_round_robin_idx = 0
_client_round_robin_lock = threading.Lock()


def _pick_client(active_clients):
    """从活跃客户端列表中轮询选一个（多打印机负载均衡），列表为空返回 None。"""
    global _client_round_robin_idx
    if not active_clients:
        return None
    with _client_round_robin_lock:
        idx = _client_round_robin_idx % len(active_clients)
        _client_round_robin_idx += 1
        return active_clients[idx]


@app.route("/api/download/<file_id>")
def download_file(file_id):
    """打印机客户端下载文件（用签名 token 验证）"""
    token = request.args.get("t", "")
    try:
        fid = download_serializer.loads(token, max_age=3600)
        if fid != file_id:
            raise BadSignature("file_id mismatch")
    except (BadSignature, SignatureExpired):
        return jsonify({"success": False, "message": "下载链接无效或已过期"}), 403

    conn = get_db()
    row = conn.execute("SELECT path, original_name FROM files WHERE id = ?", (file_id,)).fetchone()
    conn.close()

    if not row or not os.path.exists(row["path"]):
        return jsonify({"success": False, "message": "文件不存在"}), 404

    return send_file(row["path"], download_name=row["original_name"], as_attachment=True)


# ==================== 文件页数分析（统计基础）====================


def get_file_page_count(file_path, file_type=None):
    """
    根据文件路径计算页数：
    - PDF: 使用 pypdf 读取实际页数
    - DOC/DOCX: 通过 LibreOffice 转换为 PDF 后统计页数（不可用时返回 0）
    - 图片 (png/jpg/jpeg/gif/bmp/webp): 按 1 页计算
    - 其他: 默认返回 1 页
    file_path 为 None 时（文件不存在于磁盘），仅根据 file_type 判断，返回 0 表示未知
    """
    if file_type is None and file_path:
        file_type = os.path.splitext(file_path)[1].lower().lstrip(".")

    # PDF 文件：使用 pypdf 读取页数（需要文件存在于磁盘）
    if file_type in ("pdf",):
        if file_path and os.path.exists(file_path):
            try:
                from pypdf import PdfReader
                reader = PdfReader(file_path)
                page_count = len(reader.pages)
                print(f"  [PAGE] PDF 文件 {os.path.basename(file_path)}: {page_count} 页")
                return max(page_count, 1)
            except Exception as e:
                print(f"  [WARN] 读取 PDF 页数失败 ({e})，按 1 页计算")
                return 1
        else:
            print(f"  [PAGE] PDF 文件不在磁盘，默认按 1 页计算")
            return 1

    # Word 文档：交本地打印工具转换计数（Word/WPS 比服务器 LibreOffice 更可靠）
    if file_type in ("doc", "docx"):
        name = os.path.basename(file_path) if file_path else "未知文件"
        print(f"  [PAGE] {file_type.upper()} {name}: 等待本地打印工具分析页数")
        return 0  # 0 表示"待验证"，由本地打印工具通过 Word/WPS 转换后回报

    # 图片文件：按 1 页计算
    if file_type in ("png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif"):
        name = os.path.basename(file_path) if file_path else "未知文件"
        print(f"  [PAGE] 图片文件 {name}: 按 1 页计算")
        return 1

    # 默认：1 页
    print(f"  [PAGE] 未知类型 .{file_type}，默认按 1 页计算")
    return 1


# ==================== 数据库 ====================

def get_db():
    # timeout=30 + busy_timeout=30000：多线程并发写冲突时，写者最多等待 30s
    # 而不是立即抛出 sqlite3.OperationalError: database is locked。
    # （配合 init_db() 中开启的 WAL 模式，可彻底消除绝大多数锁冲突。）
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def get_user_db():
    """独立的用户数据库连接（users / license_keys 表）"""
    conn = sqlite3.connect(USER_DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _retry_on_lock(fn, *args, max_attempts=3, **kwargs):
    """在 database is locked 错误时自动重试（指数退避：0.2s / 0.4s / 0.8s）。
    后端两个写路径（pull_queued_orders 的 HTTP handler 和 APScheduler 的
    process_pending_orders）可能在并发的协程/线程中同时写入 order_files，
    即使 WAL 模式下也会短暂互斥。重试让冲突方自动等待而非报 500。"""
    import random
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_attempts - 1:
                delay = 0.2 * (2 ** attempt) + random.uniform(0, 0.05)
                time.sleep(delay)
                continue
            raise


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(AVATAR_DIR, exist_ok=True)

    # 构建/补全 MD5 文件索引（用于上传去重）
    build_md5_index()

    conn = get_db()
    user_conn = get_user_db()
    for db_conn, db_name in [(conn, "orders.db"), (user_conn, "users.db")]:
        try:
            db_conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as e:
            print(f"  [WARN] {db_name} 开启 WAL 模式失败: {e}")
        db_conn.commit()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id            TEXT PRIMARY KEY,
            original_name TEXT    NOT NULL,
            saved_name    TEXT    NOT NULL,
            path          TEXT    NOT NULL,
            size          INTEGER NOT NULL,
            created_at    TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id          TEXT,
            file             TEXT    NOT NULL,
            copies           INTEGER NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'printing',
            created_at       TEXT    NOT NULL,
            openid           TEXT    DEFAULT '',
            duplex           TEXT    DEFAULT 'on',
            page_count       INTEGER DEFAULT 1,
            price_per_page   REAL    DEFAULT 0.25,
            total_price      REAL    DEFAULT 0,
            is_free          INTEGER DEFAULT 0,
            FOREIGN KEY (file_id) REFERENCES files(id)
        )
        """
    )

    # 兼容旧数据库：添加可能不存在的新列（如果已存在则跳过）
    # 旧列（v1 迁移）
    for col, col_type, default in [
        ("openid", "TEXT", "''"),
        ("duplex", "TEXT", "'on'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # 新列（v2 统计系统迁移）
    for col, col_type, default in [
        ("page_count", "INTEGER", "1"),
        ("price_per_page", "REAL", "0.25"),
        ("total_price", "REAL", "0"),
        ("is_free", "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # v3 迁移：删除预约字段（SQLite 3.35+ 支持 DROP COLUMN）
    for col in ["reservation_date", "reservation_time"]:
        try:
            conn.execute(f"ALTER TABLE orders DROP COLUMN {col}")
            conn.commit()
            print(f"  已删除旧字段: {col}")
        except sqlite3.OperationalError:
            pass  # 字段不存在或 SQLite 版本不支持，忽略

    # v20 迁移：订单备注（用户提交时填写，≤100 字）
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN remark TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 orders.remark 列")
    except sqlite3.OperationalError:
        pass

    # ============ 用户数据库 (users.db) ============
    # 用户表
    user_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            openid      TEXT PRIMARY KEY,
            nickname    TEXT DEFAULT '',
            avatar_path TEXT DEFAULT '',
            updated_at  TEXT NOT NULL
        )
        """
    )
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'guest'")
        user_conn.commit()
        print("  已添加 users.role 列")
    except sqlite3.OperationalError:
        pass
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN temp_until TEXT DEFAULT NULL")
        user_conn.commit()
        print("  已添加 users.temp_until 列")
    except sqlite3.OperationalError:
        pass
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN theme_mode TEXT DEFAULT 'auto'")
        user_conn.commit()
        print("  已添加 users.theme_mode 列")
    except sqlite3.OperationalError:
        pass

    # 许可密钥表
    user_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS license_keys (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            key              TEXT    UNIQUE NOT NULL,
            created_by       TEXT    NOT NULL,
            used_by          TEXT    DEFAULT NULL,
            validity_minutes INTEGER NOT NULL,
            created_at       TEXT    NOT NULL,
            expires_at       TEXT    NOT NULL,
            used_at          TEXT    DEFAULT NULL
        )
        """
    )
    try:
        user_conn.execute("ALTER TABLE license_keys ADD COLUMN type TEXT DEFAULT 'temp'")
        user_conn.commit()
        print("  已添加 license_keys.type 列")
    except sqlite3.OperationalError:
        pass
    try:
        user_conn.execute("ALTER TABLE license_keys ADD COLUMN order_id INTEGER DEFAULT NULL")
        user_conn.commit()
        print("  已添加 license_keys.order_id 列")
    except sqlite3.OperationalError:
        pass
    # 密钥生命周期状态：unused → used → revoked/finished/archived。
    # 已使用的密钥作为授权记录永久保留，绝不硬删；作废/结束只改状态。
    try:
        user_conn.execute("ALTER TABLE license_keys ADD COLUMN status TEXT DEFAULT 'used'")
        user_conn.commit()
        print("  已添加 license_keys.status 列")
    except sqlite3.OperationalError:
        pass
    # 迁移纠偏：已使用（used_by 非空）的行保持 used，未使用的行标记 unused
    user_conn.execute("UPDATE license_keys SET status = 'unused' WHERE used_by IS NULL")
    user_conn.commit()

    # 用户移除记录（被谁、何时移除；重新授权时清空，密钥记录仍保留）
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN removed_at TEXT DEFAULT NULL")
        user_conn.commit()
        print("  已添加 users.removed_at 列")
    except sqlite3.OperationalError:
        pass
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN removed_by TEXT DEFAULT NULL")
        user_conn.commit()
        print("  已添加 users.removed_by 列")
    except sqlite3.OperationalError:
        pass

    # 微信账号绑定：dev 设备账号 → 微信 openid（个人认证密钥绑定后写入）
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN bound_openid TEXT DEFAULT ''")
        user_conn.commit()
        print("  已添加 users.bound_openid 列")
    except sqlite3.OperationalError:
        pass

    # 个人认证密钥表：微信账号生成 → APP 设备兑换，完成身份绑定。
    # 与 license_keys 语义分离（授权 vs 身份），已使用的记录作为绑定历史永久保留。
    user_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bind_keys (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            key        TEXT    UNIQUE NOT NULL,
            created_by TEXT    NOT NULL,
            created_at TEXT    NOT NULL,
            expires_at TEXT    NOT NULL,
            status     TEXT    NOT NULL DEFAULT 'unused',
            used_by    TEXT    DEFAULT NULL,
            used_at    TEXT    DEFAULT NULL
        )
        """
    )
    user_conn.commit()
    user_conn.close()

    # v5 迁移：订单附加服务字段（派送/紧急/首页/地址）
    for col, col_type, default in [
        ("delivery_enabled", "INTEGER", "0"),
        ("delivery_location", "TEXT", "''"),
        ("delivery_percentage", "REAL", "0"),
        ("urgency", "TEXT", "'低'"),
        ("urgency_price", "REAL", "0"),
        ("cover_page", "INTEGER", "0"),
        ("cover_page_price", "REAL", "0.10"),
        ("pickup_address", "TEXT", "''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
            print(f"  已添加 orders.{col} 列")
        except sqlite3.OperationalError:
            pass

    # v6 迁移：无障碍打印标记
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN auto_print INTEGER DEFAULT 0")
        conn.commit()
        print("  已添加 orders.auto_print 列")
    except sqlite3.OperationalError:
        pass

    # v19 迁移：无障碍打印预约形式（立即/指定时间/倒计时 → 折算为绝对时间 scheduled_at）
    for col, col_type, default in [
        ("schedule_mode", "TEXT", "'now'"),      # now | at | countdown
        ("scheduled_at", "TEXT", "''"),          # 绝对时间 "%Y-%m-%d %H:%M:%S"，仅预约单有值
        ("schedule_frozen", "INTEGER", "0"),     # 1 = 到点文件未就绪，已冻结暂停
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
            print(f"  已添加 orders.{col} 列")
        except sqlite3.OperationalError:
            pass

    # v5 迁移：订单文件子任务表（一次提交可包含多个文件，每个文件独立份数/状态）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_files (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id       INTEGER NOT NULL,
            file_id        TEXT,
            file_name      TEXT    NOT NULL,
            copies         INTEGER DEFAULT 1,
            page_count     INTEGER DEFAULT 1,
            price_per_page REAL    DEFAULT 0.25,
            total_price    REAL    DEFAULT 0,
            is_free        INTEGER DEFAULT 0,
            status         TEXT    NOT NULL DEFAULT 'printing',
            created_at     TEXT    NOT NULL
        )
        """
    )

    # v6 迁移：order_files 添加 page_range 列（指定打印页码范围，如 "1-5,7,9"）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN page_range TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 order_files.page_range 列")
    except sqlite3.OperationalError:
        pass

    # v7 迁移：order_files 添加 operator_client 列（记录领取任务的打印机客户端 ID）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN operator_client TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 order_files.operator_client 列（用于记录领取任务的打印机）")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v8 迁移：order_files 添加 duplex 列（双面打印模式从订单级下沉到文件级）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN duplex TEXT DEFAULT 'on'")
        conn.commit()
        print("  已添加 order_files.duplex 列（文件级双面打印模式）")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v12 迁移：orders 添加 order_number 列（HN20260720-0001 格式订单号）
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN order_number TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 orders.order_number 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v13 迁移：files 添加 page_count 列（缓存文件页数，避免重复转换）
    try:
        conn.execute("ALTER TABLE files ADD COLUMN page_count INTEGER DEFAULT 0")
        conn.commit()
        print("  已添加 files.page_count 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v14 迁移：files 添加 page_count_verified 列（页数是否经本地工具验证）
    try:
        conn.execute("ALTER TABLE files ADD COLUMN page_count_verified INTEGER DEFAULT 0")
        conn.commit()
        print("  已添加 files.page_count_verified 列")
    except sqlite3.OperationalError:
        pass

    # v15 迁移：order_files 添加 page_range_original / page_range_truncated 列
    for col, col_type, default in [
        ("page_range_original", "TEXT", "''"),
        ("page_range_truncated", "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE order_files ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
            print(f"  已添加 order_files.{col} 列")
        except sqlite3.OperationalError:
            pass

    # v16 迁移：files 添加 md5 列（用于本地工具 MD5 缓存命中，避免重复下载）
    try:
        conn.execute("ALTER TABLE files ADD COLUMN md5 TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 files.md5 列")
    except sqlite3.OperationalError:
        pass

    # v17 迁移：orders 添加 source 列（区分云端/本地来源）
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN source TEXT DEFAULT 'cloud'")
        conn.commit()
        print("  已添加 orders.source 列")
    except sqlite3.OperationalError:
        pass

    # v18 迁移：order_files 添加 image_orientation 列（图片打印方向: auto/landscape/portrait）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN image_orientation TEXT DEFAULT 'auto'")
        conn.commit()
        print("  已添加 order_files.image_orientation 列")
    except sqlite3.OperationalError:
        pass

    # v18 迁移：order_files 添加 reject_reason 列（打回原因）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN reject_reason TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 order_files.reject_reason 列")
    except sqlite3.OperationalError:
        pass

    # v20 迁移：order_files 添加 locked_at 列（任务被锁定/accept 的时刻，P0-1/P1-8）
    # 用于：孤儿 printing 任务回收（按锁定时间而非创建时间，避免无限 push/回退循环）
    #       与超时判定（accept 后按 accept 时刻起算反馈超时）。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(order_files)").fetchall()]
        if "locked_at" not in cols:
            conn.execute("ALTER TABLE order_files ADD COLUMN locked_at TEXT DEFAULT ''")
            conn.commit()
            print("  已添加 order_files.locked_at 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v21 迁移：files 添加 openid 列（文件归属校验，P2-10）
    # 每个上传都会在 files 表新建一行（MD5 去重只复用磁盘文件），openid 记录归属者，
    # 提交订单时校验 file_id 归属，防止跨用户引用他人文件。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
        if "openid" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN openid TEXT DEFAULT ''")
            conn.commit()
            print("  已添加 files.openid 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v23 迁移：orders.source 语义升级 —— cloud/local → wechat/app/local（发起端标记）。
    # 历史云端订单无法区分旧版 APP 还是小程序，统一按小程序（wechat）回填；
    # 新提交由客户端显式携带 client 字段（wechat/app）。
    cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "source" in cols:
        conn.execute("UPDATE orders SET source = 'wechat' WHERE source = 'cloud'")
        conn.commit()
        print("  orders.source 已升级: cloud → wechat（历史云端订单按小程序计）")

    # v22 迁移：orders 添加 client_request_id 列（提交幂等键，防双击/重试重复建单）
    # 小程序每次提交生成唯一 ID，后端对同 openid + 同 ID 且 10 分钟内的订单去重返回。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "client_request_id" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN client_request_id TEXT DEFAULT ''")
            conn.commit()
            print("  已添加 orders.client_request_id 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v24 迁移：订单归属标记 —— 谁处理了这笔订单 + 是否为管理员自行打印。
    #   owner_name     TEXT    归属管理员姓名（本地工具下拉选择；云端订单由打印机回报时盖章）
    #   is_admin_print INTEGER 1=管理员自行打印（非顾客订单）；0=顾客订单（接单打印）
    # 首次建列后一次性迁移：历史无标记订单全部记为管理员"霍楠"、管理员自行打印。
    # 注意：迁移 UPDATE 必须放在 ALTER 成功的同一分支内执行（仅在首次建列时跑一次），
    #       否则每次启动都会把新产生的未盖章云端订单（owner_name=''）错误地改记为霍楠自打。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "owner_name" not in cols and "is_admin_print" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN owner_name TEXT DEFAULT ''")
            conn.execute("ALTER TABLE orders ADD COLUMN is_admin_print INTEGER DEFAULT 0")
            conn.commit()
            conn.execute(
                "UPDATE orders SET owner_name = '霍楠', is_admin_print = 1 "
                "WHERE owner_name IS NULL OR owner_name = ''"
            )
            conn.commit()
            print("  已添加 orders.owner_name / orders.is_admin_print 列")
            print("  历史无标记订单已迁移至管理员“霍楠”名下（管理员自行打印）")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v26 迁移：orders 添加 print_started_at 列（订单实际开始打印的时刻）。
    # 用于对比「发起订单」与「订单开始打印」的时间差（数据传输/文件转换/队列等待/延迟自动打印所致）。
    # 本地工具只发 start_printing 信号不带时间，后端收到即用**服务器时钟**幂等写入（仅首次，不覆盖），
    # 与 created_at 同一时钟源 → 差值无漂移，等待时长由 calc_wait_seconds() 统一下发 wait_seconds。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "print_started_at" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN print_started_at TEXT DEFAULT ''")
            conn.commit()
            print("  已添加 orders.print_started_at 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v27 迁移：多设备接单 —— orders 记录「目标设备」(target_client) 与「实际接收设备」(received_client)。
    #   · target_client：移动端提交时指定的可接单设备（空 = 未指定，由任一接单设备接收）
    #   · received_client：任务实际被哪台设备推送/拉取/打印（首次写入，幂等）
    #   存量订单没有接收设备记录，统一回填为历史打印机「姚懿祥的设备（DESKTOP-EJGEB1V-a6c1a1365e）」。
    LEGACY_PRINTER_CLIENT = "DESKTOP-EJGEB1V-a6c1a1365e"
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        added = False
        if "received_client" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN received_client TEXT DEFAULT ''")
            added = True
        if "target_client" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN target_client TEXT DEFAULT ''")
            added = True
        if added:
            # 存量订单统一归属历史打印机（迁移时刻一次性回填；此后新订单由接收流程写入）
            conn.execute(
                "UPDATE orders SET received_client = ? WHERE received_client = ''",
                (LEGACY_PRINTER_CLIENT,),
            )
            conn.commit()
            print("  已添加 orders.received_client / orders.target_client 列，存量订单归属历史打印机")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # 确保历史打印机在设备注册表中（owner=姚懿祥），供「接单设备」列展示
    try:
        _dev = load_devices()
        if LEGACY_PRINTER_CLIENT not in _dev or not _dev[LEGACY_PRINTER_CLIENT].get("owner_name", ""):
            entry = _dev.get(LEGACY_PRINTER_CLIENT) or {}
            entry["device_name"] = entry.get("device_name", "") or "DESKTOP-EJGEB1V"
            entry["owner_name"] = entry.get("owner_name", "") or "姚懿祥"
            entry["last_seen"] = entry.get("last_seen", "")
            _dev[LEGACY_PRINTER_CLIENT] = entry
            save_devices(_dev)
            print("  设备注册表已补录历史打印机（姚懿祥）")
    except Exception:
        pass

    # 收支清算配置（单行 JSON blob，id 恒为 1；随 orders.db 一起被 backup.sh 备份）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS finance_config (
            id         INTEGER PRIMARY KEY,
            data       TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


# ==================== 订单号生成 ====================

@app.route("/api/next_order_number", methods=["GET"])
def next_order_number():
    """本地打印工具获取下一个可用订单号（需 token 认证）。
    调用即分配，同时在数据库创建 reserved 状态占位记录，
    防止订单号被浪费。若后续未调用 /api/local_orders 提交，
    超时后由定时任务自动标记为 abandoned。"""
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    order_number = generate_order_number()

    # 预留即记归属（与 local_orders 上报口径一致）：先把当前归属者写进占位单，
    # 这样即使预留后未打印上报（被标 abandoned），也不会留下 owner_name 为空的“无归属”残留。
    # owner_name/is_admin_print 可选携带：支持界面上“选中哪个归属者默认就是哪个”的语义，
    # 未携带时默认使用占位名 / 管理员自行打印（与 local_orders 的缺省值保持一致）。
    owner_name = (request.args.get("owner_name") or request.form.get("owner_name") or "").strip()
    is_admin_print_raw = request.args.get("is_admin_print") or request.form.get("is_admin_print")
    if is_admin_print_raw is not None:
        is_admin_print = 1 if str(is_admin_print_raw) not in ("", "0", "false", "False") else 0
    else:
        is_admin_print = 1  # 本地预留默认视为管理员自行打印
    owner_name = owner_name or DEFAULT_OWNER_NAME

    # 创建占位订单记录（状态 reserved），后续 /api/local_orders 会更新为 sent
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO orders (file, copies, status, created_at, openid, order_number, source,
                                   owner_name, is_admin_print)
               VALUES (?, 1, 'reserved', ?, 'local', ?, 'local', ?, ?)""",
            ("（预留位置）", created_at, order_number, owner_name, is_admin_print),
        )
        conn.commit()
    except Exception:
        pass  # 占位插入失败不影响订单号分配
    finally:
        conn.close()

    return jsonify({"success": True, "order_number": order_number})


# ==================== 订单号生成（内部函数）====================

_ORDER_COUNTER_LOCK = threading.Lock()
ORDER_COUNTER_FILE = os.path.join(UPLOAD_DIR, "order_counter.json")


def generate_order_number():
    """生成订单号 HN{YYYYMMDD}-{4位当日序号}，线程安全，跨天自动归零。"""
    today = datetime.now().strftime("%Y%m%d")

    with _ORDER_COUNTER_LOCK:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        try:
            with open(ORDER_COUNTER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"date": today, "counter": 0}

        if data.get("date") != today:
            data = {"date": today, "counter": 0}

        data["counter"] += 1
        counter = data["counter"]

        with open(ORDER_COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())  # P2-15：写盘并落盘，防断电/崩溃丢失已分配序号

    return f"HN{today}-{counter:04d}"


# ==================== 价格计算 ====================

def calculate_price(page_count, duplex, page_range=""):
    """根据有效打印页数（范围过滤后）和双面模式计算价格（单份文件，不含份数倍率）。
    单面打印: simplex_price 元/页（默认 0.2）
    双面打印: duplex_price 元/张（默认 0.3，每张纸可印两页，奇数页最后一张按单面价计费）
    价格来源 pricing.json（与 /api/pricing、前端计费显示、本地打印工具一致，
    在「收支统计 → 设置」中统一维护）；读取失败回退默认 0.2/0.3。
    page_range 为空 → 按整份页数计。"""
    if page_count <= 0:
        return 0.0
    simplex_price, duplex_price = _load_paper_prices()
    effective = _count_pages_in_range(page_range or "", page_count)
    if duplex == "on":
        sheets = math.ceil(effective / 2)
        odd_pages = effective % 2
        price = (sheets - odd_pages) * duplex_price + odd_pages * simplex_price
    else:
        price = effective * simplex_price
    return round(price, 2)


def _load_paper_prices() -> tuple[float, float]:
    """读取 pricing.json 的单双面价格，失败/非法回退默认 0.2/0.3。"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        sp = float(cfg.get("simplex_price", 0.2) or 0.2)
        dp = float(cfg.get("duplex_price", 0.3) or 0.3)
        return (sp if sp > 0 else 0.2), (dp if dp > 0 else 0.3)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return 0.2, 0.3


# ==================== 订单状态聚合 ====================

# 状态优先级：失败 > 打印中/排队中 > 已完成 > 被打回 > 已取消
# 用于把多个子任务 (order_files) 的状态聚合为父订单 (orders) 的状态
_STATUS_PRIORITY = {"failed": 9, "printing": 8, "waiting": 7, "accepted": 6,
                    "downloading": 5, "offline_unknown": 5, "scheduled": 4, "queued": 4,
                    "sent": 3, "abandoned": 2, "rejected": 1, "canceled": 0}


def aggregate_order_status(conn, order_id):
    """根据 order_files 的状态聚合父订单状态：
    - 全部 sent → sent
    - 全部 rejected → rejected
    - 任一 failed → failed（其余继续）
    - 否则取优先级最高者（printing 优先于 queued）
    - 无子任务时保持原状态
    """
    rows = conn.execute(
        "SELECT status FROM order_files WHERE order_id = ?", (order_id,)
    ).fetchall()
    if not rows:
        return None
    statuses = [r["status"] for r in rows]
    if all(s == "sent" for s in statuses):
        return "sent"
    if all(s == "rejected" for s in statuses):
        return "rejected"
    if all(s in ("sent", "accepted", "offline_unknown", "abandoned", "canceled", "rejected") for s in statuses):
        # 混合终态：用户主动取消优先于已完成——
        # 已取消订单即使有部分文件在取消前已打印完成，父订单仍显示"已取消"
        #（否则取消后迟到的 print_success 会把聚合状态刷回 sent，用户看到"已完成"）
        if any(s == "canceled" for s in statuses):
            return "canceled"
        if any(s == "sent" for s in statuses):
            return "sent"
        if any(s == "offline_unknown" for s in statuses):
            return "offline_unknown"
        if any(s == "accepted" for s in statuses):
            return "accepted"
        if any(s == "abandoned" for s in statuses):
            return "abandoned"
        if any(s == "rejected" for s in statuses):
            return "rejected"
        return "canceled"
    # 取优先级最高的非终态
    active = [s for s in statuses if s not in ("sent", "accepted", "offline_unknown", "abandoned", "canceled", "rejected")]
    if not active:
        return "sent"
    return max(active, key=lambda s: _STATUS_PRIORITY.get(s, 0))


def refresh_order_status(conn, order_id):
    """重算并写入父订单的聚合状态，返回新状态（无子任务时返回 None）"""
    new_status = aggregate_order_status(conn, order_id)
    if new_status:
        conn.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id)
        )
    return new_status


def calc_wait_seconds(created_at, print_started_at):
    """计算「下单 → 实际开始打印」的等待秒数（后端权威时钟）。

    created_at（下单时）与 print_started_at（收到 start_printing 时）两端都由服务器写入，
    故差值不存在跨设备时钟漂移；本地工具只发信号、不上报时间，杜绝客户端时钟不准导致
    等待时长失真（甚至为负）的问题。

    返回 int 秒数；任一端缺失、格式异常或结果为负（脏数据）→ None。
    """
    if not created_at or not print_started_at:
        return None
    try:
        t0 = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(str(print_started_at)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    delta = int((t1 - t0).total_seconds())
    return delta if delta >= 0 else None


def expire_stale_queued_orders():
    """防滥用：淘汰排队超过 queued_timeout_hours 的子任务（标记 failed 并刷新父订单状态）。
    目的：释放被 queued 状态钉住的磁盘文件——cleanup_expired_files 会跳过活跃状态
    （含 queued）引用的文件；若无打印机认领，攻击者可用排队订单让文件永驻服务器。
    超时淘汰后状态变为 failed（非活跃状态），清理任务即可删除对应文件。"""
    timeout_hours = _SECURITY["queued_timeout_hours"]
    if timeout_hours <= 0:
        return
    cutoff = datetime.now() - timedelta(hours=timeout_hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_db_conn()
        rows = conn.execute(
            "SELECT id, order_id FROM order_files WHERE status = 'queued' AND created_at < ?",
            (cutoff_str,),
        ).fetchall()
        if not rows:
            conn.close()
            return
        order_ids = set()
        for r in rows:
            conn.execute(
                "UPDATE order_files SET status = 'failed', reject_reason = ? WHERE id = ?",
                (f"排队超过 {timeout_hours} 小时未打印，已自动取消", r["id"]),
            )
            order_ids.add(r["order_id"])
        for oid in order_ids:
            refresh_order_status(conn, oid)
        conn.commit()
        conn.close()
    print(f"[SECURITY] 已淘汰 {len(rows)} 个排队超时子任务（> {timeout_hours} 小时）")


# ==================== 原子任务领取 ====================


def fetch_and_lock_task(client_id):
    """原子化地获取一个 queued 任务并立即锁定为 printing，返回完整任务字典或 None。
    使用全局 db_lock 确保多台打印机并发拉取时不会重复分配同一任务。
    2026-12：多设备接单 —— 只拉取「指定给本设备」的任务（target_client=本机），
    以及「未指定目标」的任务（仅启用接单的设备可拉取，其他设备不参与分配）。"""
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT of.id FROM order_files of
                   JOIN orders o ON of.order_id = o.id
                   WHERE of.status = 'queued'
                     AND (o.target_client = ? OR (o.target_client = '' AND ? = 1))
                   ORDER BY of.created_at ASC LIMIT 1""",
                (client_id, 1 if is_claiming(client_id) else 0),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            task_id = row["id"]
            # 锁定任务并记录领取者（locked_at = 锁定时刻，P0-1：孤儿回收/超时判定以它为准，
            # 不再依赖 created_at，避免"任务被反复 push/回退导致 created_at 很旧被误回收"的循环）
            conn.execute(
                "UPDATE order_files SET status = 'printing', operator_client = ?, locked_at = ? WHERE id = ?",
                (client_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
            )
            # 刷新父订单聚合状态
            parent_row = conn.execute(
                "SELECT order_id FROM order_files WHERE id = ?", (task_id,)
            ).fetchone()
            if parent_row:
                refresh_order_status(conn, parent_row["order_id"])
                # 记录实际接收设备（首次写入，幂等）
                conn.execute(
                    "UPDATE orders SET received_client = COALESCE(NULLIF(received_client, ''), ?) WHERE id = ?",
                    (client_id, parent_row["order_id"]),
                )
            conn.commit()
            # 重新查询完整数据返回（含父订单信息 + 文件 MD5）
            full_task = conn.execute(
                """SELECT of.*, o.order_number, o.delivery_enabled, o.delivery_location,
                          o.urgency, o.cover_page, o.cover_page_price, o.auto_print,
                          o.owner_name, o.is_admin_print, o.openid, o.remark, o.source,
                          f.md5 as source_md5
                   FROM order_files of
                   LEFT JOIN orders o ON of.order_id = o.id
                   LEFT JOIN files f ON of.file_id = f.id
                   WHERE of.id = ?""",
                (task_id,),
            ).fetchone()
            return dict(full_task)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ==================== 定时任务：扫描并推送打印任务 ====================


def process_pending_orders():
    """扫描排队中的子任务（order_files）：当目标打印机客户端上线时，推送排队任务。
    2026-12：多设备接单 —— 订单指定了目标设备（target_client）则只发往该设备，
    未指定则发往任一在线接单设备；目标设备离线 → 保持排队等其上线。"""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT of.id AS of_id, of.order_id, of.file_id, of.file_name,
               of.copies, of.page_range, of.duplex, of.image_orientation,
               o.auto_print, o.target_client
        FROM order_files of
        JOIN orders o ON of.order_id = o.id
        WHERE of.status = 'queued'
        ORDER BY of.created_at ASC
        """
    ).fetchall()
    conn.close()

    if not rows:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    online = set(get_active_clients())
    claiming_ids = get_claiming_device_ids()
    print(f"\n[{now_str}] 扫描到 {len(rows)} 个排队子任务, {len(online)} 个活跃客户端, "
          f"{len(claiming_ids)} 台可接单设备")

    if not claiming_ids:
        print("  无启用接单的打印机，订单保持排队（等待接单设备上线）")
        return

    for row in rows:
        of_id = row["of_id"]
        order_id = row["order_id"]
        file_id = row["file_id"]
        file_name = row["file_name"]
        copies = row["copies"]
        page_range = row["page_range"] or ""
        duplex = row["duplex"]
        target_client = row["target_client"] or ""

        # 确定目标设备：指定设备 → 仅该设备（须在接单集合且在线）；未指定 → 任一在线接单设备
        if target_client:
            if target_client not in claiming_ids or target_client not in online:
                print(f"  [WAIT] 子任务 #{of_id}: 目标设备 {target_client} 未在线/未接单，保持排队")
                continue
            dest_client = target_client
        else:
            dest_client = next((c for c in claiming_ids if c in online), None)
            if not dest_client:
                print(f"  [WAIT] 子任务 #{of_id}: 无在线接单设备，保持排队")
                continue

        # 查找文件路径
        file_path = None
        if file_id:
            conn = get_db()
            frow = conn.execute("SELECT path, original_name FROM files WHERE id = ?", (file_id,)).fetchone()
            conn.close()
            if frow and os.path.exists(frow["path"]):
                file_path = frow["path"]
                file_name = frow["original_name"]

        if not file_path:
            print(f"  [FAIL] 子任务 #{of_id}: 文件不存在")
            conn = get_db()
            _retry_on_lock(
                conn.execute,
                "UPDATE order_files SET status = 'failed' WHERE id = ? AND status = 'queued'",
                (of_id,),
            )
            refresh_order_status(conn, order_id)
            _retry_on_lock(conn.commit)
            conn.close()
            continue

        pushed = push_print_task_to_client(of_id, file_id, file_name, copies, duplex,
                                           page_range, dest_client,
                                           auto_print=bool(row["auto_print"]),
                                           image_orientation=row["image_orientation"] or "auto")
        if pushed:
            continue

        # 推送失败：保持 queued，等待下次扫描
        print(f"  [WAIT] 子任务 #{of_id}: 推送失败，保持排队")


def process_scheduled_orders():
    """预约订单扫描（每 30s）：
    - scheduled 子任务（阶段①文件尚未下发，如提交时打印机离线）→ 有在线客户端时下发文件（downloading）
    - waiting 子任务且 scheduled_at 已到、未冻结 → 向拥有该文件的客户端发 start_print 兜底
      （本地通常已按 scheduled_ts 自触发，此信号为幂等冗余，用于恢复/兜底）
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT of.id AS of_id, of.order_id, of.file_id, of.file_name,
               of.copies, of.page_range, of.duplex, of.status AS of_status,
               of.operator_client, of.image_orientation,
               o.auto_print, o.schedule_mode, o.scheduled_at, o.schedule_frozen,
               o.target_client
        FROM order_files of
        JOIN orders o ON of.order_id = o.id
        WHERE of.status IN ('scheduled', 'waiting')
          AND o.schedule_mode != 'now'
          AND o.status != 'canceled'   -- 已取消订单的子任务不再下发/兜底（P2-9）
        ORDER BY of.created_at ASC
        """
    ).fetchall()
    conn.close()
    if not rows:
        return

    active_printer = get_active_printer_client()  # 兜底：未指定目标时任一在线接单设备
    online = set(get_active_clients())
    now = datetime.now()

    for row in rows:
        order_id = row["order_id"]

        # 确定下发目标设备：指定目标 → 该设备（须在线）；未指定 → 任一在线接单设备
        dest_client = row["target_client"] or ""
        if dest_client:
            if dest_client not in online:
                continue
        else:
            dest_client = active_printer
            if not dest_client:
                continue

        if row["of_status"] == "scheduled":
            # 阶段①：文件下发（下发到目标设备/任一在线接单设备；无可用设备则保持 scheduled 等待）
            if not dest_client:
                continue
            file_id = row["file_id"]
            if not file_id:
                continue
            conn = get_db()
            frow = conn.execute("SELECT path FROM files WHERE id = ?", (file_id,)).fetchone()
            conn.close()
            if not frow or not os.path.exists(frow["path"]):
                # 文件已被清理 → 标记失败
                conn = get_db()
                _retry_on_lock(
                    conn.execute,
                    "UPDATE order_files SET status = 'failed' WHERE id = ? AND status = 'scheduled'",
                    (row["of_id"],),
                )
                refresh_order_status(conn, order_id)
                _retry_on_lock(conn.commit)
                conn.close()
                continue
            push_print_task_to_client(row["of_id"], file_id, row["file_name"],
                                      row["copies"], row["duplex"] or "on",
                                      row["page_range"] or "", dest_client,
                                      auto_print=True,
                                      scheduled_download=True,
                                      schedule_mode=row["schedule_mode"],
                                      scheduled_at=row["scheduled_at"] or "",
                                      schedule_frozen=row["schedule_frozen"],
                                      image_orientation=row["image_orientation"] or "auto")

        elif row["of_status"] == "waiting":
            # 阶段②兜底：到点且未冻结 → 发 start_print（本地通常已自触发，幂等）
            if row["schedule_frozen"]:
                continue
            sat = row["scheduled_at"] or ""
            if not sat:
                continue
            try:
                target = datetime.strptime(sat, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if target > now:
                continue
            client_id = row["operator_client"] or active_printer
            if not client_id:
                continue
            with printer_clients_lock:
                info = printer_clients.get(client_id)
                sid = info["sid"] if info else None
            if not sid:
                continue
            try:
                socketio.emit("start_print", {
                    "order_id": order_id,
                    "task_ids": [row["of_id"]],
                    "scheduled_at": sat,
                    "scheduled_ts": _iso_to_ts(sat),
                }, to=sid)
            except Exception as e:
                print(f"  [FAIL] 预约单 #{row['of_id']} start_print 兜底推送失败: {e}")


def push_print_task_to_client(sub_task_id, file_id, file_name, copies, duplex, page_range, client_id,
                              auto_print=False, scheduled_download=False,
                              schedule_mode="now", scheduled_at="", schedule_frozen=0,
                              image_orientation="auto"):
    """通过 SocketIO 推送子任务 (order_files) 到指定打印机客户端。
    sub_task_id = order_files.id。

    - 普通推送：子任务 queued → printing（登记 pushed_tasks，3 分钟超时兜底）。
    - scheduled_download=True（预约单阶段①）：子任务 scheduled → downloading，仅下发文件
      提前下载（把 scheduled_at 一并带给本地，本地到点自触发打印），不登记 pushed_tasks，
      避免"提前下发文件"被 3 分钟打印超时误杀。

    关键顺序：先在数据库中把子任务标记状态（并登记 pushed_tasks），再通过 SocketIO emit。
    这样即使 emit 之后客户端来不及回报，数据库状态也是一致的；若数据库写失败（锁等），
    任务保持原状态不会被推送，避免"客户端已处理但数据库仍旧状态"的幽灵任务。"""
    if not file_id:
        return False

    # 1. 原子锁定任务（db_lock 内使用独立连接，防止与 pull 接口并发重复推送）
    order_id = None
    with db_lock:
        conn = get_db_conn()
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if scheduled_download:
                cur = conn.execute(
                    "UPDATE order_files SET status = 'downloading', operator_client = ?, locked_at = ? WHERE id = ? AND status = 'scheduled'",
                    (client_id, now_str, sub_task_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE order_files SET status = 'printing', operator_client = ?, locked_at = ? WHERE id = ? AND status = 'queued'",
                    (client_id, now_str, sub_task_id),
                )
            # cur.rowcount == 0 表示该任务已不是 queued（正被其他流程处理或已结束）→ 跳过
            if cur.rowcount == 0:
                print(f"  [SKIP] 子任务 #{sub_task_id}: 非 queued 状态，跳过推送")
                return False
            order_row = conn.execute(
                "SELECT order_id FROM order_files WHERE id = ?", (sub_task_id,)
            ).fetchone()
            if order_row:
                order_id = order_row["order_id"]
                refresh_order_status(conn, order_id)
                # 记录实际接收设备（首次写入，幂等）
                conn.execute(
                    "UPDATE orders SET received_client = COALESCE(NULLIF(received_client, ''), ?) WHERE id = ?",
                    (client_id, order_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # 2. 登记推送等待反馈（必须在 emit 之前登记，避免回调先到导致漏清理）
    #    预约阶段①只下发文件不等待打印反馈，不登记（否则 3 分钟未打印会被超时误杀）
    if not scheduled_download:
        with pushed_tasks_lock:
            pushed_tasks[sub_task_id] = {   # key = order_files.id
                "pushed_at": datetime.now(),
                "client_id": client_id,
            }

    # 3. 取出客户端 sid 并 emit
    with printer_clients_lock:
        client_info = printer_clients.get(client_id)
        sid = client_info["sid"] if client_info else None

    download_url = make_download_url(file_id)

    # 查询订单号
    order_number = ""
    if order_id:
        conn = get_db()
        row = conn.execute(
            "SELECT order_number FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        if row:
            order_number = row["order_number"] or ""

    task_msg = {
        "type": "print_task",
        "task_id": sub_task_id,          # 语义切换为 order_files.id
        "order_id": order_id,
        "order_number": order_number,
        "file_url": download_url,
        "file_name": file_name,          # 原始文件名（含扩展名）
        "source_md5": "",               # 由后续代码填充
        "auto_print": auto_print,       # 无障碍打印：本地工具收到后自动建标签页并开始打印
        # 预约打印（无障碍打印的预约形式）：阶段①先下发文件，本地到 scheduled_ts 再开始打印
        "schedule_mode": schedule_mode,          # now | at | countdown
        "scheduled_at": scheduled_at,            # 绝对时间 "%Y-%m-%d %H:%M:%S"
        "scheduled_ts": _iso_to_ts(scheduled_at) if scheduled_at else 0,  # epoch 秒
        "schedule_frozen": int(schedule_frozen or 0),
        "options": {
            "copies": copies,
            "duplex": duplex or "on",
            "page_range": page_range or "",
            "image_orientation": image_orientation or "auto",
        },
    }

    # 查询父订单的附加服务配置，传递给本地工具
    if order_id:
        conn2 = get_db()
        o_row = conn2.execute(
            "SELECT delivery_enabled, delivery_location, urgency, cover_page, cover_page_price, owner_name, is_admin_print, openid, remark, source FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        conn2.close()
        if o_row:
            task_msg["delivery_enabled"] = bool(o_row["delivery_enabled"])
            task_msg["delivery_location"] = o_row["delivery_location"] or ""
            task_msg["urgency"] = o_row["urgency"] or "低"
            task_msg["cover_page"] = bool(o_row["cover_page"])
            task_msg["cover_page_price"] = float(o_row["cover_page_price"] or 0.10)
            # 订单备注（本地工具展示）
            task_msg["remark"] = o_row["remark"] or ""
            # v24.1：订单归属标记随任务下发，本地工具据此预勾选"管理员自行打印"
            task_msg["owner_name"] = o_row["owner_name"] or ""
            task_msg["is_admin_print"] = bool(o_row["is_admin_print"])
            # 2026-12：顾客订单的标签页归属 = 下单用户绑定的成员名（收支清算成员绑定）。
            # 管理员自行打印订单沿用 orders.owner_name（提交者昵称/前端指定）。
            task_msg["bound_owner_name"] = _get_bound_owner_name(o_row["openid"] or "")
            # 订单来源（wechat/app/local），本地工具云端任务列表展示
            task_msg["source"] = o_row["source"] or "wechat"

    # 查询文件 MD5（供本地工具 PDF 缓存命中，避免重复下载，P1-7）
    if file_id:
        conn = get_db()
        row = conn.execute("SELECT md5, path FROM files WHERE id = ?", (file_id,)).fetchone()
        conn.close()
        if row and row["md5"]:
            task_msg["source_md5"] = row["md5"]
        elif row and row["path"]:
            # 兜底：老记录 md5 列为空 → 从 MD5 索引按路径反查（load_md5_index 自带锁）
            rel = os.path.relpath(row["path"], UPLOAD_DIR)
            md5_index = load_md5_index()
            for k, v in md5_index.items():
                p = v if isinstance(v, str) else (v or {}).get("path", "")
                if p == rel:
                    task_msg["source_md5"] = k
                    break

    if not sid:
        # 客户端刚断开：预约单回退 scheduled，普通单回退 queued，等下次扫描重试
        with db_lock:
            conn = get_db_conn()
            try:
                if scheduled_download:
                    conn.execute(
                        "UPDATE order_files SET status = 'scheduled', locked_at = '' WHERE id = ? AND status = 'downloading'",
                        (sub_task_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE order_files SET status = 'queued', locked_at = '' WHERE id = ? AND status = 'printing'",
                        (sub_task_id,),
                    )
                if order_id:
                    refresh_order_status(conn, order_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        if not scheduled_download:
            with pushed_tasks_lock:
                pushed_tasks.pop(sub_task_id, None)
        print(f"  [WAIT] 子任务 #{sub_task_id}: 客户端已离线，保持排队")
        return False

    try:
        socketio.emit("print_task", task_msg, to=sid)
        print(f"  [PUSH] 子任务 #{sub_task_id}: 已推送到客户端 {client_id}")
        return True
    except Exception as e:
        print(f"  [FAIL] 子任务 #{sub_task_id}: 推送失败: {e}")
        # emit 失败：预约单回退 scheduled，普通单回退 queued，清理登记
        with db_lock:
            conn = get_db_conn()
            try:
                if scheduled_download:
                    conn.execute(
                        "UPDATE order_files SET status = 'scheduled', locked_at = '' WHERE id = ? AND status = 'downloading'",
                        (sub_task_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE order_files SET status = 'queued', locked_at = '' WHERE id = ? AND status = 'printing'",
                        (sub_task_id,),
                    )
                if order_id:
                    refresh_order_status(conn, order_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        if not scheduled_download:
            with pushed_tasks_lock:
                pushed_tasks.pop(sub_task_id, None)
        return False


def check_printing_timeout():
    """检查超过超时时间未反馈的 printing/accepted 子任务 (order_files)，标记为失败并聚合父订单。

    P1-8 超时判定（按是否已被客户端 accept 区分）：
    - 未 accept（status='printing'，打印机端窗口等待用户确认）：**不再判失败**——
      订单提交后一直未确认是常见场景（打印机端管理员未处理/忙），超时仅清 pushed_tasks
      计时条目；任务保持 printing，由 recover_orphaned_printing_tasks 5 分钟回退 queued 后
      重新推送，直到打印机确认/打回/用户取消（或排队超 queued_timeout_hours 被淘汰）。
    - 已 accept（status='accepted'）：accept 时刻（locked_at，由 accept_order 刷新）起算 3 分钟
      （PRINT_FEEDBACK_TIMEOUT）；locked_at 距今不足 3 分钟 → 跳过（仍在反馈窗口内）。
    """
    now = datetime.now()
    timeout_sub_tasks = []

    # 1) 未 accept 的任务：等待打印机确认，不判失败；仅清理 pushed_tasks 计时条目
    #    （条目只用于"推送后多久未反馈"计时，不清理会随进程一直累积）
    with pushed_tasks_lock:
        for sub_task_id, info in list(pushed_tasks.items()):
            if (now - info["pushed_at"]).total_seconds() > ACCEPT_WAIT_TIMEOUT:
                print(f"  [WAIT] 子任务 #{sub_task_id}: 推送后 {ACCEPT_WAIT_TIMEOUT // 60} 分钟未被打印机确认，"
                      f"停止计时，保持等待（不判失败）")
                del pushed_tasks[sub_task_id]

    # 2) 已 accept 的任务：locked_at（accept 时刻）起算 3 分钟
    conn = get_db()
    try:
        acc_rows = conn.execute(
            "SELECT id, order_id, locked_at FROM order_files WHERE status = 'accepted'"
        ).fetchall()
        for r in acc_rows:
            la = r["locked_at"] or ""
            if not la:
                continue  # 旧数据无 locked_at，不参与超时判定
            try:
                la_dt = datetime.strptime(la, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if (now - la_dt).total_seconds() > PRINT_FEEDBACK_TIMEOUT:
                timeout_sub_tasks.append(r["id"])

        # 3) offline_unknown（断线未知）：客户端断线且未回报结果，超时后保守标记 failed
        #    （方案 B：不自动重新入队，避免重复打印；用户可在订单列表看到失败后手动重发）
        off_rows = conn.execute(
            "SELECT id, order_id, locked_at FROM order_files WHERE status = 'offline_unknown'"
        ).fetchall()
        for r in off_rows:
            la = r["locked_at"] or ""
            if not la:
                continue  # 无 accept 时刻，不参与超时判定
            try:
                la_dt = datetime.strptime(la, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if (now - la_dt).total_seconds() <= OFFLINE_UNKNOWN_TIMEOUT:
                continue
            print(f"  [FAIL] 子任务 #{r['id']}: 断线未知超过 {OFFLINE_UNKNOWN_TIMEOUT // 60} 分钟，标记为失败")
            conn.execute(
                "UPDATE order_files SET status = 'failed', locked_at = '',"
                " reject_reason = '打印机断线，打印结果未知，已超时标记失败' WHERE id = ?",
                (r["id"],),
            )
            refresh_order_status(conn, r["order_id"])
            conn.commit()

        if not timeout_sub_tasks:
            return

        print(f"\n[TIMEOUT] 超时检查: {len(timeout_sub_tasks)} 个子任务超时")
        for sub_task_id in timeout_sub_tasks:
            of_row = conn.execute(
                "SELECT id, status, order_id, locked_at FROM order_files WHERE id = ?", (sub_task_id,)
            ).fetchone()

            if not of_row or of_row["status"] != "accepted":
                if of_row:
                    print(f"  [INFO] 子任务 #{sub_task_id}: 状态已变更为 {of_row['status']}，跳过")
                continue
            # 已 accept 但 locked_at 距今 < 3 分钟 → 刚 accept 不久，仍在反馈窗口内，跳过
            la = of_row["locked_at"] or ""
            if la:
                try:
                    la_dt = datetime.strptime(la, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    la_dt = None
                if la_dt and (now - la_dt).total_seconds() < PRINT_FEEDBACK_TIMEOUT:
                    print(f"  [INFO] 子任务 #{sub_task_id}: 刚被 accept，仍在反馈窗口内，跳过")
                    continue
            print(f"  [FAIL] 子任务 #{sub_task_id}: 已确认但超时未反馈，标记为失败")
            conn.execute(
                "UPDATE order_files SET status = 'failed', locked_at = '',"
                " reject_reason = '打印机已确认但超时未反馈打印结果' WHERE id = ? AND status = 'accepted'",
                (sub_task_id,),
            )
            refresh_order_status(conn, of_row["order_id"])
            conn.commit()
    finally:
        conn.close()


def recover_orphaned_printing_tasks():
    """扫描超过 5 分钟仍处于 printing 的任务，检查文件是否存在：
    - 文件存在 → 回退为 queued（让其他打印机重试）
    - 文件不存在 → 标记为 failed（避免无限循环）
    覆盖极端场景：服务器断电/客户端失联但未触发 disconnect 事件。

    P0-1：判定基准从 created_at 改为 locked_at（任务被锁定的时刻）。
    旧逻辑用 created_at，任务被"push → 客户端断线回滚 → 再 push"循环时会不断刷新
    created_at 附近的锁定，但 created_at 是创建时间不会变——实际问题是 created_at 很久
    的任务若被反复回退/重推，会立刻被回收造成无限循环；locked_at 只在锁定瞬间写入，
    回退时清空（locked_at=''），新锁定重新写入，语义正确且兼容旧数据（'' 视为未锁定，
    直接按超时处理）。"""
    cutoff_time = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_db_conn()
        rows = conn.execute(
            """SELECT of.id, of.order_id, of.file_id, f.path
               FROM order_files of
               LEFT JOIN files f ON of.file_id = f.id
               WHERE of.status = 'printing' AND (of.locked_at = '' OR of.locked_at < ?)""",
            (cutoff_time,)
        ).fetchall()
        if not rows:
            conn.close()
            return

        reset_ids = []  # → queued（文件还在）
        fail_ids = []   # → failed（文件已删）
        for r in rows:
            file_path = r["path"]
            if file_path and os.path.isfile(file_path):
                reset_ids.append(r["id"])
            else:
                fail_ids.append(r["id"])

        if reset_ids:
            placeholders = ",".join("?" for _ in reset_ids)
            conn.execute(
                f"UPDATE order_files SET status = 'queued', operator_client = '', locked_at = '' WHERE id IN ({placeholders})",
                [str(x) for x in reset_ids]
            )
        if fail_ids:
            placeholders = ",".join("?" for _ in fail_ids)
            conn.execute(
                f"UPDATE order_files SET status = 'failed', locked_at = '',"
                f" reject_reason = '打印机异常，任务文件已被清理，无法打印' WHERE id IN ({placeholders})",
                [str(x) for x in fail_ids]
            )

        all_ids = set(reset_ids + fail_ids)
        for r in rows:
            if r["id"] in all_ids:
                refresh_order_status(conn, r["order_id"])
        conn.commit()
        conn.close()

        if reset_ids:
            print(f"[ORPHAN] 已回收 {len(reset_ids)} 个孤儿 printing 任务 → queued")
        if fail_ids:
            print(f"[ORPHAN] 文件不存在，{len(fail_ids)} 个孤儿任务标记为 failed")


def recover_stale_downloading():
    """预约单兜底：downloading 子任务的领取客户端已不在线（如服务端重启漏了 disconnect
    事件）→ 回退 scheduled，等 process_scheduled_orders 重新下发。基于客户端在线状态判断，
    避免误伤慢速大文件下载。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, order_id, operator_client FROM order_files WHERE status = 'downloading'"
    ).fetchall()
    conn.close()
    if not rows:
        return

    active = set(get_active_clients())
    with db_lock:
        conn2 = get_db_conn()
        try:
            conn2.execute("BEGIN IMMEDIATE")
            changed = False
            for r in rows:
                if r["operator_client"] not in active:
                    # 状态守卫：SELECT 在锁外读取，防止与 cancel 竞态把已取消子任务翻回 scheduled
                    conn2.execute(
                        "UPDATE order_files SET status = 'scheduled', operator_client = '' WHERE id = ? AND status = 'downloading'",
                        (r["id"],),
                    )
                    refresh_order_status(conn2, r["order_id"])
                    changed = True
            conn2.commit()
            if changed:
                print(f"[RECOVER] {len(rows)} 个 downloading 子任务的客户端已不在线，回退 scheduled")
        except Exception:
            conn2.rollback()
            raise
        finally:
            conn2.close()


# ==================== SocketIO 事件 ====================


@socketio.on("connect")
def on_connect(auth=None):
    """打印机客户端连接 -- 验证 URL 查询参数中的 token"""
    token = _get_printer_token()
    if not token and auth and isinstance(auth, dict):
        token = auth.get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        print(f"[WARN] 打印机客户端认证失败: token 无效")
        emit("auth_fail", {"message": "token 无效"})
        disconnect()
        return False

    # P1-2.3：client_id 来源校验（非空、长度合理），避免脏数据/异常连接污染 printer_clients
    client_id = (request.args.get("client_id", request.sid) or "").strip()
    if not client_id or len(client_id) > 64:
        print(f"[WARN] 打印机客户端连接: client_id 非法，拒绝")
        emit("auth_fail", {"message": "client_id 无效"})
        disconnect()
        return False

    # 设备注册：计算机名随连接参数上报（本地工具 hostname），写入设备注册表（跨重启保留）
    device_name = (request.args.get("device_name", "") or "").strip()[:64]
    register_device(client_id, device_name)

    with printer_clients_lock:
        old = printer_clients.get(client_id)
        if old and old["sid"] != request.sid:
            # 同一 client_id 重复连接：优雅替换（旧连接断线回滚会按 sid 反查兜底），记录警告
            print(f"[WARN] 打印机客户端 {client_id} 重复连接（旧 sid={old['sid']}），已替换为新连接")
        printer_clients[client_id] = {
            "sid": request.sid,
            "heartbeat": datetime.now(),
            "connected_at": datetime.now(),
        }
    join_room(client_id)
    print(f"[LINK] 打印机客户端已连接: {client_id}")

    # 上线即同步接单状态（本机是否接管 / 当前接管者是谁），供设备端 UI 与自动续接单判断
    try:
        socketio.emit("printer_state", _printer_state_payload(client_id), to=request.sid)
    except Exception as e:
        print(f"  [CLAIM] 推送接单状态失败: {e}")

    # 打印机上线后，重推之前因离线而未能送达的页数分析请求
    # LEFT JOIN 覆盖两类待分析文件：
    #   ① 未关联任何订单的上传文件（"上传界面还在"，订单未提交）→ of.id IS NULL
    #   ② 属于活跃任务的文件（queued/printing/accepted）
    # 已取消/打回/完成的订单文件不再分析；page_count_verified=1 后自然不再命中。
    try:
        conn = get_db()
        pending = conn.execute(
            "SELECT DISTINCT f.id, f.original_name FROM files f"
            " LEFT JOIN order_files of ON f.id = of.file_id"
            " WHERE f.page_count = 0 AND f.page_count_verified = 0"
            " AND f.path != ''"
            " AND (of.id IS NULL OR of.status IN ('queued', 'printing', 'accepted'))"
            " LIMIT 20"
        ).fetchall()
        conn.close()
        pushed = 0
        for row in pending:
            fname = row["original_name"] or ""
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".doc", ".docx"):
                if request_page_analysis(row["id"], fname):
                    pushed += 1
        if pushed > 0:
            print(f"  [PAGE] 打印机上线后重推 {pushed} 个待分析文档（含未提交订单的上传文件）")
    except Exception as e:
        print(f"  [PAGE] 重推待分析文档时出错: {e}")

    # 打印机上线后，同步当前缓存保留策略（离线期间可能已变更）
    try:
        cfg = load_retention_config()
        socketio.emit("storage_config_updated", {
            "retention_days": cfg.get("days", 7),
            "retention_hours": cfg.get("hours", 0),
        }, to=request.sid)
        print(f"  [CACHE] 已同步保留策略到 {client_id}: {cfg.get('days', 7)}天{cfg.get('hours', 0)}小时")
    except Exception as e:
        print(f"  [CACHE] 同步保留策略失败: {e}")


@socketio.on("disconnect")
def on_disconnect(reason=None):
    """打印机客户端断开 — 立即回滚其名下所有 printing 任务为 queued"""
    client_id = request.args.get("client_id")
    if client_id:
        with db_lock:
            conn = get_db_conn()
            try:
                # 查找该客户端名下所有 printing 子任务 → 回滚为 queued
                rows = conn.execute(
                    "SELECT id, order_id FROM order_files WHERE status = 'printing' AND operator_client = ?",
                    (client_id,)
                ).fetchall()
                if rows:
                    ids = [str(r["id"]) for r in rows]
                    placeholders = ",".join("?" for _ in rows)
                    # P0-1.4：回退 queued 时同时清空 locked_at，防止旧锁定时间被孤儿回收误判
                    conn.execute(
                        f"UPDATE order_files SET status = 'queued', operator_client = '', locked_at = '' WHERE id IN ({placeholders}) AND status = 'printing'",
                        ids
                    )
                    for r in rows:
                        refresh_order_status(conn, r["order_id"])
                    print(f"[RECOVER] 客户端 {client_id} 断开，已回滚 {len(rows)} 个任务")

                # 预约单：文件下载中的子任务回退 scheduled（文件未接收完，可重新下发）
                dl_rows = conn.execute(
                    "SELECT id, order_id FROM order_files WHERE status = 'downloading' AND operator_client = ?",
                    (client_id,)
                ).fetchall()
                if dl_rows:
                    dl_ids = [str(r["id"]) for r in dl_rows]
                    dl_placeholders = ",".join("?" for _ in dl_rows)
                    conn.execute(
                        f"UPDATE order_files SET status = 'scheduled', operator_client = '', locked_at = '' WHERE id IN ({dl_placeholders}) AND status = 'downloading'",
                        dl_ids
                    )
                    for r in dl_rows:
                        refresh_order_status(conn, r["order_id"])
                    print(f"[RECOVER] 客户端 {client_id} 断开，{len(dl_rows)} 个下载中子任务回退 scheduled")

                # 已接受但未打印的任务：标记为"断线未知"
                accepted_rows = conn.execute(
                    "SELECT id, order_id FROM order_files WHERE status = 'accepted' AND operator_client = ?",
                    (client_id,)
                ).fetchall()
                if accepted_rows:
                    a_ids = [str(r["id"]) for r in accepted_rows]
                    a_placeholders = ",".join("?" for _ in accepted_rows)
                    conn.execute(
                        f"UPDATE order_files SET status = 'offline_unknown' WHERE id IN ({a_placeholders}) AND status = 'accepted'",
                        a_ids
                    )
                    for r in accepted_rows:
                        refresh_order_status(conn, r["order_id"])
                    print(f"[RECOVER] 客户端 {client_id} 断开，{len(accepted_rows)} 个已接受任务标记为断线未知")

                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # 清理客户端注册信息
    with printer_clients_lock:
        if client_id and client_id in printer_clients:
            del printer_clients[client_id]
        else:
            # 兜底：通过 sid 反向查找（client_id 可能不在 request.args 中）
            for cid, info in list(printer_clients.items()):
                if info["sid"] == request.sid:
                    del printer_clients[cid]
                    break
    # 更新设备最后在线时间（注册表保留离线记录，供收支清算「授权」页展示）
    if client_id:
        devices = load_devices()
        if client_id in devices:
            devices[client_id]["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_devices(devices)
    print(f"[LINK] 打印机客户端已断开: {client_id or request.sid}")


@socketio.on("ping")
def on_ping():
    """心跳 -- 更新最后心跳时间"""
    with printer_clients_lock:
        for cid, info in printer_clients.items():
            if info["sid"] == request.sid:
                info["heartbeat"] = datetime.now()
                break
    emit("pong")


def _get_event_client_id():
    """事件来源校验（P1-2）：用当前连接的 request.sid 反查 printer_clients 中的 client_id，
    查不到（连接未注册/伪造 sid）返回 None。"""
    return _find_client_id_by_sid(request.sid)


@socketio.on("print_success")
def on_print_success(data):
    """打印成功 -- 更新子任务状态为 sent，并聚合父订单状态"""
    task_id = data.get("task_id")
    if not task_id:
        return

    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        print(f"  [WARN] print_success 收到非法 task_id: {task_id!r}，已忽略")
        return

    # 来源校验（P1-2）：仅该任务的领取客户端可回报成功
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] print_success 来自未注册连接（sid={request.sid}），已忽略")
        return

    conn = get_db()
    row = conn.execute(
        "SELECT order_id, operator_client FROM order_files WHERE id = ?", (task_id,)
    ).fetchone()
    if row and row["operator_client"] and row["operator_client"] != client_id:
        print(f"  [WARN] print_success 子任务 #{task_id} 的 operator_client={row['operator_client']}"
              f" 与当前连接 {client_id} 不符，已拒绝")
        conn.close()
        return

    # P1-5：状态集加入 queued（断线回滚闭环：回滚后客户端回报成功也能收敛为 sent）；
    # P0-1.4：完成时清空 locked_at
    conn.execute(
        "UPDATE order_files SET status = 'sent', locked_at = '' WHERE id = ? AND status IN ('printing', 'accepted', 'offline_unknown', 'waiting', 'downloading', 'queued')",
        (task_id,),
    )
    # 获取父订单 ID 并刷新聚合状态
    if row:
        refresh_order_status(conn, row["order_id"])
        # 归属标记：本地打印工具回报成功时附带操作管理员姓名（订单号右侧下拉选择）。
        # 云端订单为顾客订单（is_admin_print 保持 0），仅盖章 owner_name；
        # 若客户端显式上报 is_admin_print 则一并写入。
        owner_name = (data.get("owner_name") or "").strip()
        admin_print = data.get("is_admin_print")
        if owner_name or admin_print is not None:
            if owner_name and admin_print is not None:
                conn.execute(
                    "UPDATE orders SET owner_name = ?, is_admin_print = ? WHERE id = ?",
                    (owner_name, 1 if admin_print else 0, row["order_id"]),
                )
            elif owner_name:
                conn.execute(
                    "UPDATE orders SET owner_name = ? WHERE id = ?",
                    (owner_name, row["order_id"]),
                )
    conn.commit()
    conn.close()

    with pushed_tasks_lock:
        pushed_tasks.pop(task_id, None)

    # 同步本地回报的实际打印配置（份数/双面/范围/页数）并重算价格
    _sync_subtask_config(task_id, data.get("copies"), data.get("duplex"),
                         data.get("page_range"), data.get("page_count"))

    print(f"  [OK] 子任务 #{task_id}: 客户端确认打印成功")


@socketio.on("print_fail")
def on_print_fail(data):
    """打印失败 -- 更新子任务状态为 failed，并聚合父订单状态"""
    task_id = data.get("task_id")
    error = data.get("error", "未知错误")

    if not task_id:
        return

    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        print(f"  [WARN] print_fail 收到非法 task_id: {task_id!r}，已忽略")
        return

    # 来源校验（P1-2）：仅该任务的领取客户端可回报失败
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] print_fail 来自未注册连接（sid={request.sid}），已忽略")
        return

    print(f"  [FAIL] 子任务 #{task_id}: 客户端打印失败 ({error})，标记为失败")
    conn = get_db()
    row = conn.execute(
        "SELECT order_id, operator_client FROM order_files WHERE id = ?", (task_id,)
    ).fetchone()
    if row and row["operator_client"] and row["operator_client"] != client_id:
        print(f"  [WARN] print_fail 子任务 #{task_id} 的 operator_client={row['operator_client']}"
              f" 与当前连接 {client_id} 不符，已拒绝")
        conn.close()
        return
    # P1-5：禁止 sent → failed 降级（已完成的任务不能被迟到的失败回报改状态）；
    # 同时排除 canceled——用户已取消的任务不能被迟到的失败回报覆盖成 failed
    #（与 print_success 的状态白名单对称；否则取消正在打印的订单时，本地工具对
    #  剩余任务回报的"已取消"失败会把 canceled 覆盖成 failed，父订单显示"打印失败"）
    # 失败原因写入 reject_reason（移动端订单详情/收支结算可展示）
    conn.execute(
        "UPDATE order_files SET status = 'failed', locked_at = '', reject_reason = ? WHERE id = ?"
        " AND status IN ('printing', 'accepted', 'offline_unknown', 'waiting', 'downloading', 'queued')",
        (("打印失败: " + str(error))[:200], task_id),
    )
    # 获取父订单 ID 并刷新聚合状态
    if row:
        refresh_order_status(conn, row["order_id"])
    conn.commit()
    conn.close()

    with pushed_tasks_lock:
        pushed_tasks.pop(task_id, None)


# ==================== 预约打印（无障碍打印的预约形式） ====================


def _find_client_id_by_sid(sid):
    """根据 SocketIO sid 反查 client_id（断线处理同款逻辑）"""
    with printer_clients_lock:
        for cid, info in printer_clients.items():
            if info["sid"] == sid:
                return cid
    return None


def _all_subtasks_ready(conn, order_id):
    """该预约订单的所有子任务是否已全部就绪（waiting/sent 等，无 downloading/scheduled/failed）"""
    rows = conn.execute(
        "SELECT status FROM order_files WHERE order_id = ?", (order_id,)
    ).fetchall()
    if not rows:
        return False
    return all(r["status"] in ("waiting", "sent", "accepted", "offline_unknown") for r in rows)


@socketio.on("file_ready")
def on_file_ready(data):
    """预约单阶段①文件下载完成：downloading → waiting。
    若该订单之前被冻结（到点文件未就绪），且本任务补齐后全部就绪 → 解除冻结，
    重设 scheduled_at = now + 30s 缓冲，并向本地发 start_print 让其按新目标倒计时/打印。"""
    task_ids = data.get("task_ids") or data.get("task_id")
    if isinstance(task_ids, int):
        task_ids = [task_ids]
    if not task_ids:
        return
    try:
        task_ids = [int(t) for t in task_ids]
    except (ValueError, TypeError):
        print(f"  [WARN] file_ready 收到非法 task_ids: {task_ids!r}，已忽略")
        return
    order_id = data.get("order_id")

    # 来源校验（P1-2）：仅领取该下载任务的客户端可回报就绪
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] file_ready 来自未注册连接（sid={request.sid}），已忽略")
        return

    resume_target = None
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for tid in task_ids:
                # 来源校验：operator_client 与当前连接不符的子任务拒绝处理
                chk = conn.execute(
                    "SELECT operator_client FROM order_files WHERE id = ?", (tid,)
                ).fetchone()
                if chk and chk["operator_client"] and chk["operator_client"] != client_id:
                    print(f"  [WARN] file_ready 子任务 #{tid} 的 operator_client={chk['operator_client']}"
                          f" 与当前连接 {client_id} 不符，跳过")
                    continue
                row = conn.execute(
                    "SELECT order_id FROM order_files WHERE id = ? AND status = 'downloading'", (tid,)
                ).fetchone()
                if not row:
                    continue
                # 父订单已取消 → 迟到的下载完成不恢复为 waiting，直接保持 canceled（防已取消订单复活）
                o = conn.execute(
                    "SELECT status FROM orders WHERE id = ?", (row["order_id"],)
                ).fetchone()
                if o and o["status"] == "canceled":
                    print(f"  [SKIP] file_ready 子任务 #{tid}: 父订单已取消，保持 canceled")
                    continue
                conn.execute(
                    "UPDATE order_files SET status = 'waiting' WHERE id = ? AND status = 'downloading'",
                    (tid,),
                )
                refresh_order_status(conn, row["order_id"])
            if order_id:
                o = conn.execute(
                    "SELECT schedule_mode, schedule_frozen FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
                if o and o["schedule_mode"] != "now" and o["schedule_frozen"] and _all_subtasks_ready(conn, order_id):
                    new_target = datetime.now() + timedelta(seconds=30)
                    new_target_str = new_target.strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "UPDATE orders SET schedule_frozen = 0, scheduled_at = ? WHERE id = ?",
                        (new_target_str, order_id),
                    )
                    resume_target = new_target_str
                    print(f"  [RESUME] 订单 #{order_id} 冻结解除，重设 {new_target_str}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    if resume_target and order_id:
        emit("start_print", {"order_id": order_id, "task_ids": task_ids,
                             "scheduled_at": resume_target,
                             "scheduled_ts": _iso_to_ts(resume_target)})
    print(f"  [READY] 预约单文件就绪: {task_ids}")


@socketio.on("download_delayed")
def on_download_delayed(data):
    """预约单到点文件未就绪 → 冻结订单（暂停倒计时）。
    本地文件补齐后会重新发 file_ready，届时解除冻结并重设目标。"""
    task_ids = data.get("task_ids") or data.get("task_id")
    if isinstance(task_ids, int):
        task_ids = [task_ids]
    order_id = data.get("order_id")
    if not order_id:
        return

    # 来源校验（P1-2）：冻结操作必须来自该订单下载中任务的领取客户端
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] download_delayed 来自未注册连接（sid={request.sid}），已忽略")
        return
    conn_chk = get_db()
    chk_rows = conn_chk.execute(
        "SELECT operator_client FROM order_files WHERE order_id = ? AND status = 'downloading'",
        (order_id,),
    ).fetchall()
    conn_chk.close()
    if chk_rows and any(r["operator_client"] and r["operator_client"] != client_id for r in chk_rows):
        print(f"  [WARN] download_delayed 订单 #{order_id} 的下载任务不属于当前连接 {client_id}，已拒绝")
        return

    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            o = conn.execute(
                "SELECT schedule_mode, status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            # 父订单已取消 → 不再冻结（子任务已取消，无需续排）
            if o and o["schedule_mode"] != "now" and o["status"] != "canceled":
                conn.execute(
                    "UPDATE orders SET schedule_frozen = 1, scheduled_at = '' WHERE id = ?",
                    (order_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    print(f"  [FREEZE] 订单 #{order_id} 到点文件未就绪，已冻结")
    emit("download_delayed_ack", {"order_id": order_id, "frozen": 1})


@socketio.on("start_printing")
def on_start_printing(data):
    """本地工具开始打印：记录订单实际开始打印的时刻（print_started_at，幂等写入），
    并驱动预约单 waiting/downloading → printing，登记 pushed_tasks
    启用 3 分钟超时兜底（与普通单一致）。断网时本地也可能直接打完后报 print_success，
    那时走 print_success 的 waiting 兼容分支。

    print_started_at 一律取**服务器时钟**（收到本事件的时刻），不接受客户端上报的时间：
    客户端时钟可能不准，且与 created_at（服务器时钟）跨设备相减会得出错误的等待时长。
    等待时长由后端用 calc_wait_seconds() 统一计算后下发 wait_seconds 字段。"""
    task_ids = data.get("task_ids") or data.get("task_id")
    if isinstance(task_ids, int):
        task_ids = [task_ids]
    if not task_ids:
        return
    try:
        task_ids = [int(t) for t in task_ids]
    except (ValueError, TypeError):
        print(f"  [WARN] start_printing 收到非法 task_ids: {task_ids!r}，已忽略")
        return

    # 来源校验（P1-2）：仅领取该预约下载任务的客户端可上报开始打印
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] start_printing 来自未注册连接（sid={request.sid}），已忽略")
        return

    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            started_at = now_str  # 服务器时钟为唯一权威时间源
            for tid in task_ids:
                # 来源校验：operator_client 与当前连接不符的子任务跳过
                chk = conn.execute(
                    "SELECT operator_client FROM order_files WHERE id = ?", (tid,)
                ).fetchone()
                if chk and chk["operator_client"] and chk["operator_client"] != client_id:
                    print(f"  [WARN] start_printing 子任务 #{tid} 的 operator_client={chk['operator_client']}"
                          f" 与当前连接 {client_id} 不符，跳过")
                    continue
                row = conn.execute(
                    "SELECT order_id FROM order_files WHERE id = ?", (tid,)
                ).fetchone()
                if not row:
                    continue
                # 父订单已取消 → 不再开始打印（防已取消订单复活）
                o = conn.execute(
                    "SELECT status FROM orders WHERE id = ?", (row["order_id"],)
                ).fetchone()
                if o and o["status"] == "canceled":
                    print(f"  [SKIP] start_printing 子任务 #{tid}: 父订单已取消，跳过")
                    continue
                # 记录实际开始打印时刻（幂等：仅首次，不覆盖早于本次上报的开始时间）
                conn.execute(
                    "UPDATE orders SET print_started_at = ? WHERE id = ?"
                    " AND (print_started_at IS NULL OR print_started_at = '')",
                    (started_at, row["order_id"]),
                )
                conn.execute(
                    "UPDATE order_files SET status = 'printing', operator_client = ?, locked_at = ? WHERE id = ? AND status IN ('waiting', 'downloading')",
                    (client_id, now_str, tid),
                )
                refresh_order_status(conn, row["order_id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    with pushed_tasks_lock:
        for tid in task_ids:
            pushed_tasks[tid] = {"pushed_at": datetime.now(), "client_id": client_id}
    print(f"  [START] 本地工具开始打印: {task_ids}")


@socketio.on("page_count_result")
def on_page_count_result(data):
    """本地打印工具回报文件页数分析结果。
    data: {file_id, page_count, orientation, success}"""
    file_id = data.get("file_id", "")
    try:
        page_count = int(data.get("page_count", 0) or 0)
    except (ValueError, TypeError):
        page_count = 0
    orientation = data.get("orientation", "")
    success = data.get("success", True)

    # 来源校验（P1-2）：仅已注册的打印机客户端可回报页数（file_id 维度无法核对
    # operator_client，因为页数分析是广播式领取的；至少保证连接合法）
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] page_count_result 来自未注册连接（sid={request.sid}），已忽略")
        return

    if not file_id or page_count <= 0:
        print(f"  [PAGE] page_count_result 无效: file_id={file_id}, pages={page_count}")
        return

    conn = get_db()
    conn.execute(
        "UPDATE files SET page_count = ?, page_count_verified = 1 WHERE id = ?",
        (page_count, file_id),
    )
    if page_count == 1:
        # 单页文件无法双面：用户在未知页数时可能选了双面 →
        # 强制引用该文件的活跃子任务改为单面，后端显示与价格随之一致
        conn.execute(
            "UPDATE order_files SET duplex = 'off' WHERE file_id = ?"
            " AND status IN ('queued', 'printing', 'accepted', 'waiting', 'downloading')",
            (file_id,),
        )
    # 同步更新 MD5 索引：下次同 MD5 文件上传直接复用页数，无需再次分析
    row = conn.execute("SELECT path FROM files WHERE id = ?", (file_id,)).fetchone()
    conn.commit()
    conn.close()
    if row and row["path"] and os.path.exists(row["path"]):
        try:
            file_md5 = get_file_md5(row["path"])
            md5_index = load_md5_index()
            _md5_entry_set(md5_index, file_md5, page_count=page_count, page_count_verified=True)
            save_md5_index(md5_index)
            print(f"  [PAGE] MD5 索引已更新: {file_md5[:8]}... → {page_count} 页")
        except Exception as e:
            print(f"  [WARN] 更新 MD5 页数缓存失败: {e}")
    print(f"  [PAGE] ✓ 本地工具回报: {file_id[:8]}... → {page_count} 页 ({orientation})")

    # 验证使用该文件的所有活跃子任务的页码范围是否超出总页数
    _validate_page_ranges_for_file(file_id, page_count)

    # P2-1（docx 少计费）：页数修正后回溯更新引用该文件的子任务页数与价格、父订单总额
    _recalc_prices_for_file(file_id, page_count)


def _recalc_prices_for_file(file_id, page_count):
    """页数回报后回溯修正子任务价格与父订单总额（P2-1）：
    对引用该文件、状态未终结（非 sent/failed/canceled/rejected/abandoned）的子任务，
    用新页数重算 total_price（calculate_price × copies），并重算父订单 total_price
    （基础打印费 + 附加服务费，口径与 submit_order 一致），同时刷新聚合状态。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, order_id, copies, duplex, page_range FROM order_files"
            " WHERE file_id = ? AND status NOT IN ('sent','failed','canceled','rejected','abandoned')",
            (file_id,),
        ).fetchall()
        if not rows:
            return
        order_ids = set()
        for r in rows:
            per_copy = calculate_price(page_count, r["duplex"] or "on", r["page_range"] or "")
            new_total = round(per_copy * (r["copies"] or 1), 2)
            conn.execute(
                "UPDATE order_files SET page_count = ?, total_price = ? WHERE id = ?",
                (page_count, new_total, r["id"]),
            )
            order_ids.add(r["order_id"])
        for oid in order_ids:
            sub_sum = conn.execute(
                "SELECT COALESCE(SUM(total_price), 0) FROM order_files WHERE order_id = ?",
                (oid,),
            ).fetchone()[0]
            o = conn.execute(
                "SELECT delivery_enabled, delivery_percentage, urgency_price, cover_page, cover_page_price"
                " FROM orders WHERE id = ?",
                (oid,),
            ).fetchone()
            if o:
                delivery_fee = round(float(sub_sum) * (o["delivery_percentage"] or 0) / 100, 2) if o["delivery_enabled"] else 0
                urgency_fee = float(o["urgency_price"] or 0)
                cover_fee = round(float(o["cover_page_price"] or 0) * o["cover_page"], 2) if o["cover_page"] else 0
                parent_total = round(float(sub_sum) + urgency_fee + cover_fee + delivery_fee, 2)
                conn.execute("UPDATE orders SET total_price = ? WHERE id = ?", (parent_total, oid))
            refresh_order_status(conn, oid)
        conn.commit()
        print(f"  [PRICE] 已回溯重算 {len(rows)} 个子任务的价格（页数修正为 {page_count} 页）")
    except Exception as e:
        print(f"  [WARN] 价格回溯重算失败: {e}")
        conn.rollback()
    finally:
        conn.close()


def _sync_subtask_config(task_id, copies, duplex, page_range, page_count):
    """本地回报实际打印配置后，同步 order_files 并重算子任务价格与父订单总额。
    场景：用户在小程序选了份数/双面，本地打印工具可能在打印前修改（含单页强制单面），
    打印成功回报时带上实际配置，后端据此更新记录与价格。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT id, order_id FROM order_files WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            return
        set_parts = []
        params = []
        if copies is not None:
            try:
                c = int(copies)
                if 1 <= c <= 999:
                    set_parts.append("copies = ?"); params.append(c)
            except (ValueError, TypeError):
                pass
        if duplex in ("on", "off"):
            set_parts.append("duplex = ?"); params.append(duplex)
        if page_range is not None:
            set_parts.append("page_range = ?"); params.append(str(page_range)[:500])
        if page_count is not None:
            try:
                pc = int(page_count)
                if pc > 0:
                    set_parts.append("page_count = ?"); params.append(pc)
            except (ValueError, TypeError):
                pass
        if not set_parts:
            conn.close()
            return
        params.append(task_id)
        conn.execute(f"UPDATE order_files SET {', '.join(set_parts)} WHERE id = ?", params)
        # 用更新后的值重算子任务价格
        cur = conn.execute(
            "SELECT page_count, copies, duplex, page_range FROM order_files WHERE id = ?", (task_id,)
        ).fetchone()
        if cur:
            per_copy = calculate_price(cur["page_count"] or 1, cur["duplex"] or "on", cur["page_range"] or "")
            new_total = round(per_copy * (cur["copies"] or 1), 2)
            conn.execute("UPDATE order_files SET total_price = ? WHERE id = ?", (new_total, task_id))
            # 重算父订单总额（基础打印费 + 附加服务费，口径与 submit_order 一致）
            oid = row["order_id"]
            sub_sum = conn.execute(
                "SELECT COALESCE(SUM(total_price), 0) FROM order_files WHERE order_id = ?", (oid,)
            ).fetchone()[0]
            o = conn.execute(
                "SELECT delivery_enabled, delivery_percentage, urgency_price, cover_page, cover_page_price"
                " FROM orders WHERE id = ?", (oid,)
            ).fetchone()
            if o:
                delivery_fee = round(float(sub_sum) * (o["delivery_percentage"] or 0) / 100, 2) if o["delivery_enabled"] else 0
                urgency_fee = float(o["urgency_price"] or 0)
                cover_fee = round(float(o["cover_page_price"] or 0) * o["cover_page"], 2) if o["cover_page"] else 0
                parent_total = round(float(sub_sum) + urgency_fee + cover_fee + delivery_fee, 2)
                conn.execute("UPDATE orders SET total_price = ? WHERE id = ?", (parent_total, oid))
            refresh_order_status(conn, oid)
        conn.commit()
        print(f"  [SYNC] 子任务 #{task_id} 实际打印配置已同步后端")
    except Exception as e:
        print(f"  [WARN] 子任务配置同步失败: {e}")
        conn.rollback()
    finally:
        conn.close()


def _parse_page_range_max(range_str: str) -> int:
    """解析页码范围字符串（如 '1-5,7,9'），返回最大页码。空字符串或无效返回 0。"""
    if not range_str or not range_str.strip():
        return 0
    max_page = 0
    for part in range_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                max_page = max(max_page, int(a.strip()), int(b.strip()))
            except ValueError:
                pass
        else:
            try:
                max_page = max(max_page, int(part))
            except ValueError:
                pass
    return max_page


def _parse_page_range_set(range_str: str, total_pages: int) -> set:
    """解析页码范围字符串为页码集合（对齐本地 printer_config._parse_range_parts）。
    支持 '1-5,7,9'、中文逗号/顿号分隔、智能拆分 '23-4' → {2,3,4}；越界部分忽略。"""
    pages: set = set()
    if not range_str or not range_str.strip():
        return pages
    raw = range_str.strip().replace("、", ",").replace("，", ",").replace("；", ",").replace(" ", "")
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
                if start < end:
                    for p in range(start, end + 1):
                        if 1 <= p <= total_pages:
                            pages.add(p)
                elif start > end and len(a) > 1:
                    # 智能拆分: "23-4" → 页码 2 + 范围 3-4
                    prefix = int(a[:-1])
                    last = int(a[-1])
                    if prefix < end:
                        for p in range(last, end + 1):
                            if 1 <= p <= total_pages:
                                pages.add(p)
                        if 1 <= prefix <= total_pages:
                            pages.add(prefix)
            except ValueError:
                pass
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    pages.add(p)
            except ValueError:
                pass
    return pages


def _count_pages_in_range(page_range: str, total_pages: int) -> int:
    """返回页码范围覆盖的有效打印页数；空范围返回总页数（与本地 calc_cost 同口径）。"""
    if not page_range or not page_range.strip():
        return total_pages
    pages = _parse_page_range_set(page_range, total_pages)
    return len(pages) if pages else total_pages


def _validate_page_ranges_for_file(file_id: str, page_count: int):
    """检查使用指定文件的所有活跃子任务，若页码范围超出总页数则自动打回。"""
    conn = get_db()
    tasks = conn.execute(
        "SELECT id, order_id, page_range FROM order_files"
        " WHERE file_id = ? AND status IN ('queued', 'printing')",
        (file_id,),
    ).fetchall()

    rejected = 0
    rejected_order_ids = set()
    for t in tasks:
        max_page = _parse_page_range_max(t["page_range"] or "")
        if max_page > page_count:
            conn.execute(
                "UPDATE order_files SET status = 'rejected', reject_reason = ? WHERE id = ?",
                (f"页码范围超出文件总页数（填写 {max_page} 页，文件共 {page_count} 页）", t["id"]),
            )
            rejected += 1
            rejected_order_ids.add(t["order_id"])

    if rejected > 0:
        conn.commit()
        for oid in rejected_order_ids:
            refresh_order_status(conn, oid)
        conn.commit()
        print(f"  [REJECT] 页数验证完成: {file_id[:8]}... → 打回 {rejected} 个子任务（{len(rejected_order_ids)} 个订单），页数 {page_count}")
    conn.close()


@socketio.on("page_range_truncated")
def on_page_range_truncated(data):
    """本地打印工具回报：某任务的页码范围被截断。
    data: {task_id, original_range, effective_range, total_pages}"""
    task_id = data.get("task_id")
    original = data.get("original_range", "")
    effective = data.get("effective_range", "")
    total_pages = data.get("total_pages", 0)

    if not task_id:
        return

    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        print(f"  [WARN] page_range_truncated 收到非法 task_id: {task_id!r}，已忽略")
        return
    print(f"  [TRUNC] 子任务 #{task_id}: 页码范围被截断 {original} → {effective} (总 {total_pages} 页)")

    conn = get_db()
    conn.execute(
        "UPDATE order_files SET page_range_original = ?, page_range_truncated = 1, page_range = ? WHERE id = ?",
        (original, effective, task_id),
    )
    conn.commit()
    conn.close()


# ── 在线设备日志收集（日志管理页发起：后端下发 request_log → 各在线设备回报本机日志）──
_collect_logs_lock = threading.Lock()
_collect_logs: dict = {}      # client_id → 设备回报的日志内容
_collect_request_id = 0       # 区分多次收集请求，防旧回报串台
_COLLECT_LOG_MAX_BYTES = 200 * 1024  # 每台设备最多回报 200KB 尾部日志


@socketio.on("logs")
def on_device_logs(data):
    """在线设备回报日志（配合 /api/log/collect_all 下发的 request_log 事件）。"""
    client_id = _find_client_id_by_sid(request.sid)
    if not client_id:
        return
    if not isinstance(data, dict):
        return
    # 仅接受本次收集请求（request_id 匹配）的回报，防上一轮迟到的回报串台
    with _collect_logs_lock:
        if data.get("request_id") != _collect_request_id:
            return
        content = str(data.get("content", "") or "")
        _collect_logs[client_id] = content[:_COLLECT_LOG_MAX_BYTES]


# ==================== 页面分析请求（推送到本地打印工具）====================


def _notify_clients(event: str, data: dict):
    """向所有活跃的打印机客户端广播事件（如储存配置更新、清空缓存等）。"""
    active = get_active_clients()
    if not active:
        return
    for client_id in active:
        with printer_clients_lock:
            info = printer_clients.get(client_id)
            sid = info["sid"] if info else None
        if sid:
            try:
                socketio.emit(event, data, to=sid)
            except Exception as e:
                print(f"  [NOTIFY] 推送 {event} 到 {client_id} 失败: {e}")


def request_page_analysis(file_id: str, file_name: str) -> bool:
    """请求本地打印工具下载文件并分析页数。成功推送返回 True。"""
    client_id = get_active_printer_client()  # 页数分析只发给接单设备（它将负责打印）
    if not client_id:
        print(f"  [PAGE] 无启用接单的在线打印机，跳过页数分析请求")
        return False

    # 幽灵文件校验：物理文件已清理/路径已清空 → 跳过（下载必然 404，
    # 避免打印机上线补推 / 前端轮询触发时反复推送已清理文件刷屏）
    try:
        conn = get_db()
        prow = conn.execute(
            "SELECT path, page_count, page_count_verified, md5 FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        conn.close()
        if not prow or not prow["path"] or not os.path.isfile(prow["path"]):
            print(f"  [PAGE] 文件已清理，跳过页数分析: {file_name} (file_id={file_id[:8]}...)")
            return False
        # 防御性自检：DB 已存验证页数 → 不再发起分析（各调用路径可能遗漏此判断，
        # 例如 MD5 索引丢失但 files 表仍有验证值）
        if prow["page_count_verified"] and (prow["page_count"] or 0) > 0:
            print(f"  [PAGE] 页数已验证 ({prow['page_count']} 页)，跳过分析: {file_name}")
            return False
        file_md5 = prow["md5"] or ""
    except Exception:
        return False

    # 统一去重（30s）：on_connect 补推 / 前端 file_page 轮询触发 / 上传共用此字典，
    # 避免同一文件在短时间内被多个路径重复推送（"补推 + 轮询"双保险不能变成双推）。
    # 幽灵校验在前（幽灵不占防抖槽）；有效推送记录时间戳。
    with _last_page_analysis_push_lock:
        last = _last_page_analysis_push.get(file_id, 0)
        now = time.time()
        if now - last < 30:
            print(f"  [PAGE] 30s 内已推送过 {file_name}，跳过重复分析请求")
            return False
        _last_page_analysis_push[file_id] = now
        if len(_last_page_analysis_push) > 5000:
            _last_page_analysis_push.clear()   # 超限清空，防止字典无限增长

    download_url = make_download_url(file_id)

    with printer_clients_lock:
        client_info = printer_clients.get(client_id)
        sid = client_info["sid"] if client_info else None

    if not sid:
        return False

    # 记录推送目标 sid，供取消分析时精确转发（同一 file_id 重新推送会覆盖为最新 sid）
    with _last_page_analysis_push_lock:
        _analysis_push_sid[file_id] = sid

    try:
        socketio.emit("analyze_page_count", {
            "file_id": file_id,
            "file_name": file_name,
            "download_url": download_url,
            "source_md5": file_md5,   # 本地可直接按 MD5 命中 PDF 缓存，无需下载文件
        }, to=sid)
        print(f"  [PAGE] 已推送页数分析请求: {file_name} (file_id={file_id[:8]}...) → {client_id}")
        return True
    except Exception as e:
        print(f"  [PAGE] 推送分析请求失败: {e}")
        return False


# ==================== 认证 ====================


def login_required(f):
    """装饰器：验证 Bearer token，将 openid 注入 g.openid"""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"success": False, "message": "未登录，请先授权"}), 401
        token = auth[7:]  # "Bearer " 之后的内容
        try:
            g.openid = token_serializer.loads(token, max_age=TOKEN_MAX_AGE)
        except SignatureExpired:
            return jsonify({"success": False, "message": "登录已过期，请重新登录"}), 401
        except BadSignature:
            return jsonify({"success": False, "message": "无效的登录凭证"}), 401
        return f(*args, **kwargs)

    return decorated


def require_printer_access(f):
    """装饰器：验证登录 + 检查非访客（需管理员或许可用户）"""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"success": False, "message": "未登录，请先授权"}), 401
        token = auth[7:]
        try:
            g.openid = token_serializer.loads(token, max_age=TOKEN_MAX_AGE)
        except SignatureExpired:
            return jsonify({"success": False, "message": "登录已过期，请重新登录"}), 401
        except BadSignature:
            return jsonify({"success": False, "message": "无效的登录凭证"}), 401

        role = compute_role(g.openid)
        if role not in ("admin", "user"):
            # 检查临时授权（temp_until > 当前时间）
            # （P2-17）时区说明：temp_until 为服务器本地时间，依赖服务器时区，
            # 部署时须配置为 Asia/Shanghai
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = get_user_db()
            row = conn.execute(
                "SELECT temp_until FROM users WHERE openid = ?", (g.openid,)
            ).fetchone()
            conn.close()
            if not row or not row["temp_until"] or row["temp_until"] <= now_str:
                return jsonify({"success": False, "message": "请先出示管理员许可"}), 403
        g.user_role = role
        return f(*args, **kwargs)

    return decorated


# ==================== API 路由 ====================


@app.before_request
def limit_json_body():
    """P2-11：JSON 请求体上限 1MB（全局 MAX_CONTENT_LENGTH=50MB 只防文件上传撑爆内存，
    JSON 接口正常数据远小于 1MB，超大 JSON 多为异常请求）。multipart 文件上传不受影响。"""
    if request.is_json:
        cl = request.content_length or 0
        if cl > 1024 * 1024:
            return jsonify({"success": False, "message": "请求体过大"}), 413


@app.route("/api/ping")
def ping():
    return {"msg": "pong", "status": "ok"}


@app.route("/api/printer_status", methods=["GET"])
def printer_status():
    """返回打印机在线状态（P2-13：基于心跳统计 get_active_clients，剔除心跳超时的僵尸注册）。
    2026-12：多设备接单 —— claiming_count 为启用接单的设备数（含离线），
    take_orders_online 表示是否有在线接单设备。"""
    active = get_active_clients()
    online_count = len(active)
    claiming_ids = get_claiming_device_ids()
    online_claiming = [c for c in claiming_ids if c in set(active)]
    return jsonify({
        "success": True,
        "online": online_count > 0,
        "active": online_count > 0,
        "count": online_count,
        "client_count": online_count,
        # 接单设备（多设备集合）
        "take_orders_online": len(online_claiming) > 0,
        "claiming_count": len(claiming_ids),
        "active_client_id": online_claiming[0] if online_claiming else "",
        "active_owner_name": get_device_owner(online_claiming[0]) if online_claiming else "",
    })


@app.route("/api/pricing", methods=["GET"])
def get_pricing():
    """返回打印定价配置（地点、优先级、首页费等），前端加载后与本地打印工具保持一致。"""
    pricing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
    try:
        with open(pricing_path, "r", encoding="utf-8") as f:
            pricing = json.load(f)
        return jsonify({"success": True, "pricing": pricing})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return jsonify({"success": False, "message": f"定价配置不可用: {e}"}), 500


@app.route("/api/pricing", methods=["POST"])
def save_pricing():
    """更新打印定价配置（pricing.json，需 printer token 认证）。
    价格权威源：小程序/APP 计费显示、后端订单计价（calculate_price / submit_order）、
    本地打印工具打印首页全部从 pricing.json 读取，因此统一在此处维护。
    body: { "pricing": { ...完整 pricing.json 结构... } }（兼容直接传 pricing 对象）
    """
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    payload = data.get("pricing", data)
    if not isinstance(payload, dict):
        return jsonify({"success": False, "message": "缺少 pricing"}), 400

    pricing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
    # 读取现有配置作为缺失字段的兜底（允许部分更新）
    try:
        with open(pricing_path, "r", encoding="utf-8") as f:
            current = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        current = {}

    merged = dict(current)

    # P1-4：边界钳制 + 类型校验，非法值直接 400 拒绝（不写盘）。
    # 选择性合并：标量字段按 payload 覆盖；delivery_percentages / urgency_prices 为空 dict
    # 时视为未修改（保留现有），防御前端误传空表清空服务器配置。
    try:
        for _k in ("simplex_price", "duplex_price", "cover_page_price", "pickup_address"):
            if _k in payload:
                merged[_k] = payload[_k]
        merged["simplex_price"] = max(0.01, min(99.99, float(merged.get("simplex_price", 0.2))))
        merged["duplex_price"] = max(0.01, min(99.99, float(merged.get("duplex_price", 0.3))))
        merged["cover_page_price"] = max(0.0, min(99.99, float(merged.get("cover_page_price", 0.1))))
        merged["pickup_address"] = str(merged.get("pickup_address", "") or "")[:100]
        dp = payload.get("delivery_percentages")
        if dp is not None and not isinstance(dp, dict):
            return jsonify({"success": False, "message": "delivery_percentages 格式不正确"}), 400
        if dp:
            merged["delivery_percentages"] = {
                str(k): max(0.0, min(100.0, float(v))) for k, v in dp.items()
            }
        merged["delivery_locations"] = [str(x) for x in (merged.get("delivery_locations") or list(merged.get("delivery_percentages", {}).keys()))]
        up = payload.get("urgency_prices")
        if up is not None and not isinstance(up, dict):
            return jsonify({"success": False, "message": "urgency_prices 格式不正确"}), 400
        if up:
            ordered_up = {str(k): max(0.0, min(99.99, float(v))) for k, v in up.items()}
            # 按档位业务顺序重排（低→中→高），未知档位按原顺序追加在后
            sorted_up = {lvl: ordered_up[lvl] for lvl in ("低", "中", "高") if lvl in ordered_up}
            for _k, _v in ordered_up.items():
                if _k not in sorted_up:
                    sorted_up[_k] = _v
            merged["urgency_prices"] = sorted_up
        merged["urgency_levels"] = [str(x) for x in (merged.get("urgency_levels") or list(merged.get("urgency_prices", {}).keys()))]
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "定价参数格式不正确"}), 400

    # 原子写盘：先写临时文件再替换，避免进程中断留下半个 pricing.json
    tmp_path = pricing_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, pricing_path)
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({"success": False, "message": "定价配置写入失败"}), 500

    print(f"定价配置已更新: 单面={merged['simplex_price']} 双面={merged['duplex_price']} "
          f"首页费={merged['cover_page_price']} 派送地点={len(merged['delivery_percentages'])} 个")
    return jsonify({"success": True, "message": "定价配置已更新", "pricing": merged})


@app.route("/api/file_page/<file_id>", methods=["GET"])
@login_required
def get_file_page(file_id):
    """查询文件的页数信息（供前端轮询本地工具分析结果）。
    返回 page_count 和 page_count_verified 标志。

    惰性触发：打印机在线但文件页数仍未知（上传时离线、补推遗漏）→ 主动补推分析请求
    （去重由 request_page_analysis 内部统一 30s 防抖，与 on_connect 补推共享）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT page_count, page_count_verified, original_name, path FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"success": False, "message": "文件不存在"}), 404
    printer_online = is_printer_available()  # 2026-11：接单设备在线才算打印机可用
    if (printer_online and not row["page_count_verified"] and (row["page_count"] or 0) <= 0):
        fname = row["original_name"] or ""
        ext = os.path.splitext(fname)[1].lower()
        if ext in (".doc", ".docx"):
            # 统一防抖在 request_page_analysis 内部：30s 内已被补推/轮询推过则跳过
            request_page_analysis(file_id, fname)
    # v4.4：PDF 等后端可直接数页的类型，若历史/异常导致页数缺失，轮询时服务端补数——
    # 不依赖本地打印工具（本地工具离线也不影响这类文件确认页数）
    if (row["page_count"] or 0) <= 0 and not row["page_count_verified"]:
        ext = os.path.splitext(row["original_name"] or "")[1].lower()
        if ext and ext not in (".doc", ".docx"):
            fpath = os.path.join(UPLOAD_DIR, row["path"]) if row.get("path") else ""
            if fpath and os.path.exists(fpath):
                new_count = get_file_page_count(fpath, ext.lstrip("."))
                if new_count > 0:
                    conn = get_db()
                    conn.execute(
                        "UPDATE files SET page_count = ?, page_count_verified = 1 WHERE id = ?",
                        (new_count, file_id),
                    )
                    conn.commit()
                    conn.close()
                    row["page_count"] = new_count
                    row["page_count_verified"] = True
    return jsonify({
        "success": True,
        "page_count": row["page_count"] or 0,
        "verified": bool(row["page_count_verified"]),
        "printer_online": printer_online,
    })


@app.route("/api/cancel_page_analysis", methods=["POST"])
@login_required
def cancel_page_analysis():
    """取消页数分析（小程序删除文件 / 关闭页面时调用）。

    对每个 file_id：清空 30s 防抖槽（避免取消后重新轮询被误拦），
    并向曾接收该分析请求的打印机客户端转发 cancel_page_analysis 事件，
    让本地工具中止正在进行的下载/转换。未记录目标时广播兜底。"""
    data = request.get_json(silent=True) or {}
    file_ids = data.get("file_ids", [])
    if not isinstance(file_ids, list) or not file_ids:
        return jsonify({"success": False, "message": "缺少 file_ids"}), 400

    with _last_page_analysis_push_lock:
        for fid in file_ids:
            _last_page_analysis_push.pop(fid, None)      # 清防抖槽，允许立即重新发起
            sid = _analysis_push_sid.pop(fid, None)
            try:
                if sid:
                    socketio.emit("cancel_page_analysis", {"file_id": fid}, to=sid)
                else:
                    socketio.emit("cancel_page_analysis", {"file_id": fid})  # 广播兜底
                print(f"  [PAGE] 已请求取消页数分析: {fid[:8]}... → sid={sid}")
            except Exception as e:
                print(f"  [WARN] 取消分析转发失败: {e}")
    return jsonify({"success": True})


@app.route("/api/login", methods=["POST"])
def wx_login():
    """微信小程序登录：用 code 换取 openid 并返回 token"""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    code = data.get("code", "")
    if not code:
        return jsonify({"success": False, "message": "缺少 code 参数"}), 400

    if not WECHAT_APPID or not WECHAT_APPSECRET:
        return jsonify({"success": False, "message": "服务器未配置微信 AppID/AppSecret"}), 500

    # 调用微信 jscode2session 接口
    params = urlparse.urlencode(
        {
            "appid": WECHAT_APPID,
            "secret": WECHAT_APPSECRET,
            "js_code": code,
            "grant_type": "authorization_code",
        }
    )
    api_url = f"https://api.weixin.qq.com/sns/jscode2session?{params}"

    try:
        req = urlrequest.Request(api_url)
        with urlrequest.urlopen(req, timeout=10) as resp:
            wx_data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return jsonify({"success": False, "message": f"调用微信接口失败: {str(e)}"}), 502

    if "errcode" in wx_data and wx_data["errcode"] != 0:
        return jsonify(
            {
                "success": False,
                "message": f"微信登录失败: {wx_data.get('errmsg', '未知错误')}",
                "errcode": wx_data["errcode"],
            }
        ), 400

    openid = wx_data["openid"]
    # session_key 不返回给前端 ---- 服务端解密用户数据时使用
    token = token_serializer.dumps(openid)

    # 首次登录：创建用户记录并分配默认昵称
    conn = get_user_db()
    existing = conn.execute("SELECT openid FROM users WHERE openid = ?", (openid,)).fetchone()
    if not existing:
        # 生成唯一默认昵称: user_ + 8位随机字母数字
        for _ in range(10):
            suffix = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
            nickname = f"user_{suffix}"
            dup = conn.execute("SELECT openid FROM users WHERE nickname = ?", (nickname,)).fetchone()
            if not dup:
                break
        conn.execute(
            "INSERT INTO users (openid, nickname, avatar_path, updated_at) VALUES (?, ?, '', ?)",
            (openid, nickname, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    conn.close()

    print(f"用户登录成功: openid={openid[:8]}...")

    return jsonify(
        {
            "success": True,
            "message": "登录成功",
            "token": token,
            "openid": openid,
        }
    )


@app.route("/api/device_login", methods=["POST"])
def device_login():
    """Android/Web 设备登录：用 device_id 换取设备 token，无需微信。

    惰性注册（方案 A）：首次登录只签发 guest 级设备 token，**不**在 users 表建号，
    避免"打开即建号"产生一堆从未使用的空账号。
    真正使用（兑换授权密钥 /api/license/redeem）时才会在 users 表惰性创建账号。
    """
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    device_id = (data.get("device_id") or "").strip()

    # 限速（P0-2.2 / P1-6 / 防滥用）：每 IP / 每 device_id 每小时登录次数上限（管理员可调），
    # 防止无限刷 token 刷库。注意：内存计数仅单 worker 有效，nginx 层应再加 IP 维度限速。
    ip = request.remote_addr or "unknown"
    _dev_login_limit = _SECURITY["device_login_rate_limit"]
    if not _rate_limit(f"device_login:ip:{ip}", _dev_login_limit, 3600) or \
       not _rate_limit(f"device_login:dev:{device_id}", _dev_login_limit, 3600):
        return jsonify({"success": False, "message": "注册过于频繁，请稍后再试"}), 429

    # P0-2.2：device_id 最小长度从 6 提高到 10，降低穷举碰撞概率
    if not device_id or len(device_id) < 10:
        return jsonify({"success": False, "message": "device_id 无效（至少 10 位）"}), 400

    # 用 device_id 生成稳定的设备 openid
    dev_openid = "dev_" + hashlib.sha256(device_id.encode()).hexdigest()[:24]

    conn = get_user_db()
    existing = conn.execute(
        "SELECT openid, bound_openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone()

    registered = bool(existing)      # users 表是否已有该设备账号
    if existing:
        # 设备已绑定微信账号 → 直接签发微信账号的 token（同一账号身份、同一权限）
        bound_openid = existing["bound_openid"] or ""
        token_openid = bound_openid or dev_openid
    else:
        # 惰性注册：不建号，签发 guest 级设备 token。compute_role 对不存在的 openid 返回 guest，
        # 直到真正兑换授权密钥（/api/license/redeem）时才惰性建号。
        bound_openid = ""
        token_openid = dev_openid

    token = token_serializer.dumps(token_openid)
    conn.close()

    print(f"设备登录: device_id={device_id[:12]}..., openid={token_openid[:8]}..., bound={bool(bound_openid)}, registered={registered}")

    return jsonify({
        "success": True,
        "token": token,
        "openid": token_openid,
        "bound": bool(bound_openid),
        "registered": registered,
    })


# ==================== 微信账号绑定（个人认证密钥） ====================

_BIND_KEY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_BIND_DEVICE_LIMIT = 3   # 每微信账号最多绑定的设备数
_BIND_UNUSED_LIMIT = 3   # 每微信账号同时未使用的密钥数上限


@app.route("/api/bind/create", methods=["POST"])
@login_required
def bind_key_create():
    """任意登录用户生成个人认证密钥：在 APP 端填写后，设备账号绑定到当前微信账号。
    密钥 1-10 分钟有效、一次性；已绑定设备数与未使用密钥数均有上限。
    """
    if g.openid.startswith("dev_"):
        return jsonify({"success": False, "message": "请使用微信小程序生成绑定密钥"}), 400

    # 限速（P1-6）：每用户每小时最多生成 5 个
    if not _rate_limit(f"bind_create:{g.openid}", 5, 3600):
        return jsonify({"success": False, "message": "生成过于频繁，请稍后再试"}), 429

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    data = request.get_json() or {}
    validity = int(data.get("validity_minutes", 5))
    validity = max(1, min(10, validity))
    expires = now + timedelta(minutes=validity)

    conn = get_user_db()
    # 已绑定设备数上限（status='used' 的绑定密钥即绑定关系记录）
    used_count = conn.execute(
        "SELECT COUNT(*) FROM bind_keys WHERE created_by = ? AND status = 'used'",
        (g.openid,),
    ).fetchone()[0]
    if used_count >= _BIND_DEVICE_LIMIT:
        conn.close()
        return jsonify({"success": False, "message": f"绑定设备已达上限（{_BIND_DEVICE_LIMIT} 台），请先解绑"}), 403
    # 未使用密钥数上限
    unused_count = conn.execute(
        "SELECT COUNT(*) FROM bind_keys WHERE created_by = ? AND status = 'unused' AND expires_at > ?",
        (g.openid, now_str),
    ).fetchone()[0]
    if unused_count >= _BIND_UNUSED_LIMIT:
        conn.close()
        return jsonify({"success": False, "message": "未使用密钥过多，请等待过期后再生成"}), 429

    # 生成唯一密钥（全量查重 + UNIQUE 约束双保险）
    bind_key = ""
    for _ in range(20):
        candidate = ''.join(secrets.choice(_BIND_KEY_ALPHABET) for _ in range(8))
        dup = conn.execute("SELECT id FROM bind_keys WHERE key = ?", (candidate,)).fetchone()
        if not dup:
            bind_key = candidate
            break
    if not bind_key:
        conn.close()
        return jsonify({"success": False, "message": "生成失败，请重试"}), 500

    conn.execute(
        """INSERT INTO bind_keys (key, created_by, created_at, expires_at, status)
           VALUES (?, ?, ?, ?, 'unused')""",
        (bind_key, g.openid, now_str, expires.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

    print(f"用户 {g.openid[:8]}... 生成个人认证密钥: {bind_key}, 有效期 {validity} 分钟")
    return jsonify({
        "success": True,
        "key": bind_key,
        "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"),
        "validity_minutes": validity,
    })


@app.route("/api/bind/redeem", methods=["POST"])
@login_required
def bind_key_redeem():
    """APP 设备账号兑换个人认证密钥：绑定到密钥创建者的微信账号。
    - 仅 dev_ 设备账号可兑换（微信账号自身不可兑换）
    - 兑换成功后设备账号的历史订单/文件迁移到微信账号，来源标记为 APP
    - 返回微信账号的新 token，APP 切换到微信账号身份
    """
    if not g.openid.startswith("dev_"):
        return jsonify({"success": False, "message": "请使用 APP 设备账号完成绑定"}), 400

    if not _rate_limit(f"bind_redeem:{g.openid}", _SECURITY["redeem_rate_limit"], 60):
        return jsonify({"success": False, "message": "兑换过于频繁，请稍后再试"}), 429

    data = request.get_json() or {}
    raw_key = (data.get("key", "") or "").strip().upper()
    if len(raw_key) != 8:
        return jsonify({"success": False, "message": "密钥格式不正确"}), 400

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dev_openid = g.openid
    conn = get_user_db()

    # 设备账号不可重复绑定（users.bound_openid 或 bind_keys 历史记录任一命中即已绑定；
    # 兼容早期 bug 只把绑定写入 bind_keys 的脏数据，避免重复绑定产生多条 used 记录）
    dev_row = conn.execute(
        "SELECT bound_openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone()
    if dev_row and dev_row["bound_openid"]:
        conn.close()
        return jsonify({"success": False, "message": "该设备已绑定微信账号，请先解绑"}), 400
    if conn.execute(
        "SELECT id FROM bind_keys WHERE used_by = ? AND status = 'used'", (dev_openid,)
    ).fetchone():
        conn.close()
        return jsonify({"success": False, "message": "该设备已绑定微信账号，请先解绑"}), 400

    # 原子认领：仅未使用且未过期才生效
    conn.execute(
        """UPDATE bind_keys SET status = 'used', used_by = ?, used_at = ?
           WHERE key = ? AND status = 'unused' AND expires_at > ?""",
        (dev_openid, now_str, raw_key, now_str),
    )
    conn.commit()
    if conn.total_changes == 0:
        row = conn.execute(
            "SELECT used_by, expires_at FROM bind_keys WHERE key = ?", (raw_key,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "message": "密钥不存在"}), 404
        if row["used_by"] is not None:
            return jsonify({"success": False, "message": "密钥已被使用"}), 400
        return jsonify({"success": False, "message": "密钥已过期"}), 400

    key_row = conn.execute(
        "SELECT created_by FROM bind_keys WHERE key = ?", (raw_key,)
    ).fetchone()
    wx_openid = key_row["created_by"] if key_row else ""

    # 写入绑定关系。注意 /api/device_login 是惰性注册：设备从未兑换授权密钥/更新资料时，
    # users 表没有该 dev 账号的行，直接 UPDATE 会静默影响 0 行——绑定关系只落在 bind_keys，
    # 导致解绑/重命名时查 users.bound_openid 为空、报"该设备未绑定微信账号"。
    # 因此先补齐 users 行，再写 bound_openid。
    if not conn.execute(
        "SELECT openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone():
        dev_nickname = "device_" + ''.join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        conn.execute(
            "INSERT INTO users (openid, nickname, avatar_path, updated_at) VALUES (?, ?, '', ?)",
            (dev_openid, dev_nickname, now_str),
        )
    conn.execute(
        "UPDATE users SET bound_openid = ?, updated_at = ? WHERE openid = ?",
        (wx_openid, now_str, dev_openid),
    )
    conn.commit()
    conn.close()

    # 历史订单/文件迁移：dev 账号此前的订单并入微信账号（统计"同一用户"口径连续）。
    # 先回填旧版 APP 订单来源为 app，再迁移归属。
    orders_conn = get_db()
    try:
        orders_conn.execute(
            "UPDATE orders SET source = 'app' WHERE openid = ? AND source = 'wechat'",
            (dev_openid,),
        )
        orders_conn.execute(
            "UPDATE orders SET openid = ? WHERE openid = ?", (wx_openid, dev_openid)
        )
        orders_conn.execute(
            "UPDATE files SET openid = ? WHERE openid = ?", (wx_openid, dev_openid)
        )
        orders_conn.commit()
    except Exception:
        orders_conn.rollback()
        raise
    finally:
        orders_conn.close()

    # 返回微信账号 token，APP 切换身份
    token = token_serializer.dumps(wx_openid)
    profile_conn = get_user_db()
    prow = profile_conn.execute(
        "SELECT nickname FROM users WHERE openid = ?", (wx_openid,)
    ).fetchone()
    profile_conn.close()
    nickname = (prow["nickname"] if prow else "") or ""

    print(f"设备 {dev_openid[:10]}... 已绑定微信账号 {wx_openid[:8]}...（密钥 {raw_key}）")
    return jsonify({
        "success": True,
        "message": "绑定成功，APP 已切换到微信账号身份",
        "token": token,
        "openid": wx_openid,
        "dev_openid": dev_openid,
        "nickname": nickname,
        "role": compute_role(wx_openid),
    })


@app.route("/api/bind/devices", methods=["GET"])
@login_required
def bind_devices():
    """查询当前微信账号已绑定的设备列表（含设备昵称、绑定时间、密钥）"""
    conn = get_user_db()
    rows = conn.execute(
        """SELECT b.used_by AS dev_openid, b.used_at, b.key, u.nickname
           FROM bind_keys b
           LEFT JOIN users u ON b.used_by = u.openid
           WHERE b.created_by = ? AND b.status = 'used'
           ORDER BY b.used_at DESC""",
        (g.openid,),
    ).fetchall()
    conn.close()
    devices = [{
        "dev_openid": r["dev_openid"] or "",
        "nickname": r["nickname"] or "",
        "key": r["key"],
        "used_at": r["used_at"] or "",
    } for r in rows]
    return jsonify({"success": True, "devices": devices, "device_count": len(devices)})


@app.route("/api/bind/rename", methods=["POST"])
@login_required
def bind_rename():
    """小程序重命名已绑定设备：仅绑定关系中的微信账号可操作。
    名称写入该 dev_ 账号的 users.nickname，作为设备在“已绑定设备”列表中的展示名。
    body: { "dev_openid": "dev_...", "nickname": "办公室平板" }
    """
    data = request.get_json() or {}
    dev_openid = (data.get("dev_openid", "") or "").strip()
    nickname = (data.get("nickname", "") or "").strip()
    if not dev_openid.startswith("dev_"):
        return jsonify({"success": False, "message": "设备账号无效"}), 400
    if not nickname:
        return jsonify({"success": False, "message": "名称不能为空"}), 400
    if len(nickname) > 20:
        return jsonify({"success": False, "message": "名称最长 20 个字符"}), 400

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    row = conn.execute(
        "SELECT bound_openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone()
    if not row or not row["bound_openid"]:
        # 兜底：兼容早期绑定只写入 bind_keys 的脏数据（users 行缺失或 bound_openid 为空），
        # 与解绑逻辑一致，以 bind_keys 中 status='used' 的记录作为绑定关系判据。
        bk = conn.execute(
            "SELECT created_by FROM bind_keys WHERE used_by = ? AND status = 'used' "
            "ORDER BY used_at DESC LIMIT 1",
            (dev_openid,),
        ).fetchone()
        if not bk:
            conn.close()
            return jsonify({"success": False, "message": "该设备未绑定微信账号"}), 404
        bound_wx = bk["created_by"]
    else:
        bound_wx = row["bound_openid"]
    if g.openid != bound_wx:
        conn.close()
        return jsonify({"success": False, "message": "仅账号本人可重命名"}), 403

    # 写入昵称；设备从未建号时先补齐 users 行（bound_openid 一并写入，保持两处绑定关系一致）
    if not row:
        conn.execute(
            "INSERT INTO users (openid, nickname, avatar_path, bound_openid, updated_at) "
            "VALUES (?, ?, '', ?, ?)",
            (dev_openid, nickname, bound_wx, now_str),
        )
    else:
        conn.execute(
            "UPDATE users SET nickname = ?, updated_at = ? WHERE openid = ?",
            (nickname, now_str, dev_openid),
        )
    conn.commit()
    conn.close()

    print(f"微信账号 {g.openid[:8]}... 重命名设备 {dev_openid[:10]}... → {nickname}")
    return jsonify({"success": True, "nickname": nickname})


@app.route("/api/bind/revoke", methods=["POST"])
@login_required
def bind_revoke():
    """解除设备绑定：仅绑定关系中的微信账号可操作（小程序本人或已绑定的 APP）。
    解绑后设备账号回退为独立 dev_ 账号；APP 侧可拿到新的 dev_ token 切换身份。
    """
    data = request.get_json() or {}
    dev_openid = (data.get("dev_openid", "") or "").strip()
    if not dev_openid.startswith("dev_"):
        return jsonify({"success": False, "message": "设备账号无效"}), 400

    conn = get_user_db()
    row = conn.execute(
        "SELECT bound_openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone()
    if not row or not row["bound_openid"]:
        # 兜底：兼容早期绑定只写入 bind_keys 的脏数据（设备从未建号，或建号晚于绑定导致
        # users.bound_openid 为空），以 bind_keys 中 status='used' 的记录作为绑定关系判据，
        # 保证已绑定设备始终可解绑。
        bk = conn.execute(
            "SELECT created_by FROM bind_keys WHERE used_by = ? AND status = 'used' "
            "ORDER BY used_at DESC LIMIT 1",
            (dev_openid,),
        ).fetchone()
        if not bk:
            conn.close()
            return jsonify({"success": False, "message": "该设备未绑定微信账号"}), 404
        wx_openid = bk["created_by"]
    else:
        wx_openid = row["bound_openid"]
    if g.openid != wx_openid:
        conn.close()
        return jsonify({"success": False, "message": "仅账号本人可解除绑定"}), 403

    conn.execute(
        "UPDATE users SET bound_openid = '' WHERE openid = ?", (dev_openid,)
    )
    conn.execute(
        """UPDATE bind_keys SET status = 'revoked'
           WHERE created_by = ? AND used_by = ? AND status = 'used'""",
        (wx_openid, dev_openid),
    )
    conn.commit()
    conn.close()

    dev_token = token_serializer.dumps(dev_openid)
    print(f"微信账号 {wx_openid[:8]}... 已解绑设备 {dev_openid[:10]}...")
    return jsonify({
        "success": True,
        "message": "已解除绑定",
        "token": dev_token,
        "openid": dev_openid,
    })


@app.route("/api/upload", methods=["POST"])
@require_printer_access
def upload_file():
    # 限速（P1-6 / 防滥用）：每用户每分钟最多上传次数（阈值管理员可调，单 worker 内存计数）
    if not _rate_limit(f"upload:{g.openid}", _SECURITY["upload_rate_limit"], 60):
        return jsonify({"success": False, "message": "上传过于频繁，请稍后再试"}), 429

    # 磁盘剩余空间守卫（防存储型 DDoS）：低于阈值直接拒绝，避免磁盘被写满
    min_free_bytes = _SECURITY["disk_min_free_mb"] * 1024 * 1024
    if min_free_bytes > 0:
        try:
            free_bytes = shutil.disk_usage(UPLOAD_DIR).free
            if free_bytes < min_free_bytes:
                return jsonify({
                    "success": False,
                    "message": "服务器存储空间不足，请稍后再试",
                    "free_mb": round(free_bytes / (1024 * 1024), 1),
                }), 503
        except OSError:
            pass  # 磁盘信息不可用时放行，交由配额兜底

    if "file" not in request.files:
        return jsonify({"success": False, "message": "未找到上传文件"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "message": "文件名为空"}), 400

    file_id = uuid.uuid4().hex
    ext = os.path.splitext(f.filename)[1]  # 保留原始大小写
    ext_lower = ext.lower().lstrip(".")
    # P2-9：扩展名白名单正则校验（1-8 位字母数字），非法 → 400（原实现会把非法扩展名
    # 直接拼进路径导致 500 或目录穿越风险）
    if not re.match(r"^[a-zA-Z0-9]{1,8}$", ext_lower):
        return jsonify({"success": False, "message": "文件扩展名无效"}), 400
    saved_name = f"{file_id}{ext}"

    # 1. 先保存到 uploads/ 根目录作为临时文件
    temp_path = os.path.join(UPLOAD_DIR, saved_name)
    f.save(temp_path)
    file_size = os.path.getsize(temp_path)

    # 防滥用：每用户累计配额检查（含本次文件；超限拒绝并清理临时文件，防存储型 DDoS）。
    # 注：MD5 复用文件也计入配额——同一文件被多个用户引用时各算一份，偏保守但更安全。
    quota_bytes = _SECURITY["user_quota_mb"] * 1024 * 1024
    if quota_bytes > 0:
        conn = get_db()
        used = conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS used FROM files WHERE openid = ? AND path != ''",
            (g.openid,),
        ).fetchone()["used"] or 0
        conn.close()
        if used + file_size > quota_bytes:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            return jsonify({
                "success": False,
                "message": "存储配额已满，请删除旧文件后再上传",
                "quota_mb": _SECURITY["user_quota_mb"],
                "used_mb": round(used / (1024 * 1024), 1),
            }), 413

    # 2. 计算 MD5 并查重
    file_md5 = get_file_md5(temp_path)
    md5_index = load_md5_index()
    existing_entry = _md5_entry_get(md5_index, file_md5)

    reused = False
    cached_page_count = 0
    cached_page_verified = False

    if existing_entry:
        existing_rel = existing_entry.get("path", "")
        existing_path = os.path.join(UPLOAD_DIR, existing_rel) if existing_rel else ""
        if existing_path and os.path.exists(existing_path):
            # MD5 命中且文件存在 → 复用
            os.remove(temp_path)
            file_path = existing_path
            file_size = os.path.getsize(file_path)
            saved_name = os.path.basename(existing_path)
            reused = True
            # 读取缓存的页数
            cached_page_count = existing_entry.get("page_count", 0) or 0
            cached_page_verified = existing_entry.get("page_count_verified", False)
            print(f"  [MD5] 文件复用: {f.filename} → {existing_rel} (MD5={file_md5[:8]}..."
                  f"{', 页数已验证=' + str(cached_page_count) if cached_page_verified else ''})")
        else:
            # 索引记录存在但磁盘文件丢失 → 清理索引，走新文件保存
            del md5_index[file_md5]
            save_md5_index(md5_index)
            existing_entry = None
            print(f"  [MD5] 索引记录失效（文件丢失），重新保存: {existing_rel}")

    if not reused:
        # 3. 确定扩展名子目录，移动文件
        subdir = get_ext_dir(ext_lower)
        target_dir = os.path.join(UPLOAD_DIR, subdir)
        os.makedirs(target_dir, exist_ok=True)
        final_path = os.path.join(target_dir, saved_name)
        if os.path.exists(final_path):
            saved_name = f"{file_id}_{uuid.uuid4().hex[:6]}{ext}"
            final_path = os.path.join(target_dir, saved_name)
        shutil.move(temp_path, final_path)
        file_path = final_path

        # 4. 更新 MD5 索引（新格式：含原始文件名）
        rel_path = os.path.relpath(file_path, UPLOAD_DIR)
        _md5_entry_set(md5_index, file_md5, path=rel_path, original_name=f.filename,
                        page_count=0, page_count_verified=False)
        save_md5_index(md5_index)
        print(f"  [MD5] 新增索引: {file_md5[:8]}... → {rel_path}")
    else:
        # 更新 MD5 索引中的原始文件名（可能不同用户上传了不同命名的同一文件）
        _md5_entry_set(md5_index, file_md5, original_name=f.filename)
        save_md5_index(md5_index)

    # 5. 计算/复用文件页数
    if cached_page_verified and cached_page_count > 0:
        # 同 MD5 文件已由本地工具验证 → 直接复用页数，跳过分析
        page_count = cached_page_count
        print(f"  [PAGE] 复用已验证页数: {f.filename} → {page_count} 页 (from MD5 cache)")
    else:
        page_count = get_file_page_count(file_path, ext_lower)

    # 6. 写入 files 表（含 page_count 缓存、md5、openid 归属，P2-10）
    conn = get_db()
    conn.execute(
        """
        INSERT INTO files (id, original_name, saved_name, path, size, created_at, page_count, page_count_verified, md5, openid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, f.filename, saved_name, file_path, file_size,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), page_count,
         int(cached_page_verified), file_md5, g.openid),
    )
    conn.commit()
    conn.close()

    # 对需要本地工具分析的文档格式，请求直连的打印客户端下载并分析页数
    # 但若同 MD5 已有验证页数，跳过分析
    if page_count == 0 and ext_lower in ("doc", "docx") and not cached_page_verified:
        request_page_analysis(file_id, f.filename)

    print(f"文件上传: {f.filename} -> {file_path} (id={file_id}, pages={page_count}, reused={reused})")

    return jsonify(
        {
            "success": True,
            "message": "文件上传成功" if not reused else "文件已存在，直接使用",
            "file_id": file_id,
            "original_name": f.filename,
            "size": file_size,
            "page_count": page_count,
            "reused": reused,
        }
    )


@app.route("/api/submit_order", methods=["POST"])
@require_printer_access
def submit_order():
    # 限速（P1-6 / 防滥用）：每用户每分钟提交订单次数上限（管理员可调，单 worker 内存计数）
    if not _rate_limit(f"submit_order:{g.openid}", _SECURITY["submit_order_rate_limit"], 60):
        return jsonify({"success": False, "message": "提交过于频繁，请稍后再试"}), 429

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    # 发起端标记：小程序 / APP（写入 orders.source，供统计页区分下单渠道）。
    # 绑定后 APP 与小程序共享 openid，必须依赖显式标记而非 openid 前缀。
    client = (data.get("client", "wechat") or "wechat").strip().lower()
    if client not in ("wechat", "app"):
        client = "wechat"

    # 幂等键（P0-2）：客户端每次提交生成唯一 ID；同 openid + 同 ID 且 10 分钟内
    # 已成功建单 → 直接返回原订单，防双击/网络重试造成重复订单。
    client_request_id = (data.get("client_request_id", "") or "").strip()

    duplex = data.get("duplex", "on")  # 顶层 duplex 作为默认值（向后兼容）
    files_input = data.get("files", None)

    # ---- v5 新增：附加服务参数 ----
    # P1-4：全部做边界钳制（delivery/urgency/cover 均为非负金额或百分比），
    # 非法类型（ValueError/TypeError）→ 400 明确错误。
    try:
        delivery_enabled = int(data.get("delivery_enabled", 0) or 0)
        delivery_location = data.get("delivery_location", "")
        delivery_percentage = max(0.0, min(100.0, float(data.get("delivery_percentage", 0) or 0)))
        urgency = data.get("urgency", "低")
        urgency_price = max(0.0, min(100.0, float(data.get("urgency_price", 0) or 0)))
        cover_page = int(data.get("cover_page", 0) or 0)
        cover_page_price = max(0.0, min(100.0, float(data.get("cover_page_price", 0.10) or 0)))
        pickup_address = data.get("pickup_address", "")
        auto_print = int(data.get("auto_print", 0) or 0)  # 无障碍打印：提交后自动开始打印
        # 订单备注：≤100 字，多余截断
        remark = str(data.get("remark", "") or "").strip()[:100]
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "附加服务参数格式不正确"}), 400

    # P1-4.7：服务端价格优先——pricing.json 中存在的配置项覆盖客户端金额字段
    # （delivery 按地点百分比、urgency 按档位价、cover 按统一首页费），忽略客户端传入值。
    pricing_cfg = _get_pricing_config()
    if pricing_cfg:
        if delivery_enabled and delivery_location in (pricing_cfg.get("delivery_percentages") or {}):
            delivery_percentage = float(pricing_cfg["delivery_percentages"][delivery_location])
        elif not delivery_enabled:
            delivery_percentage = 0.0
        if urgency in (pricing_cfg.get("urgency_prices") or {}):
            urgency_price = float(pricing_cfg["urgency_prices"][urgency])
        if cover_page and "cover_page_price" in pricing_cfg:
            cover_page_price = float(pricing_cfg["cover_page_price"])

    # ---- 无障碍打印预约：立即/指定时间/倒计时 → 折算为绝对时间 scheduled_at ----
    # （P2-17）scheduled_at 为服务器本地时间字符串，依赖服务器时区；
    # 部署时必须将服务器时区设为 Asia/Shanghai，否则预约到点判定偏移。
    schedule_mode = (data.get("schedule_mode", "now") or "now").strip()
    if schedule_mode not in ("now", "at", "countdown"):
        schedule_mode = "now"
    scheduled_at = ""
    if schedule_mode == "at":
        # 指定时间：日期（0=今天 1=明天 2=后天）+ HH:MM
        schedule_day = int(data.get("schedule_day", 0) or 0)
        schedule_time = (data.get("schedule_time", "") or "").strip()
        m = re.match(r"^(\d{1,2}):(\d{2})$", schedule_time)
        if not m:
            return jsonify({"success": False, "message": "请选择预约时间"}), 400
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59 or schedule_day not in (0, 1, 2):
            return jsonify({"success": False, "message": "预约时间格式不正确"}), 400
        target = (datetime.now() + timedelta(days=schedule_day)).replace(
            hour=hh, minute=mm, second=0, microsecond=0)
        if target <= datetime.now():
            return jsonify({"success": False, "message": "预约时间已过，请重新选择"}), 400
        scheduled_at = target.strftime("%Y-%m-%d %H:%M:%S")
    elif schedule_mode == "countdown":
        # 倒计时：___分___秒
        # P1-4.5：范围 1 秒 ~ 7 天（604800），非法（含 0/负数/非数字）→ 400
        try:
            countdown_seconds = int(data.get("countdown_seconds", 0) or 0)
        except (ValueError, TypeError):
            return jsonify({"success": False, "message": "倒计时时长必须是数字"}), 400
        if not (1 <= countdown_seconds <= 604800):
            return jsonify({"success": False, "message": "倒计时时长必须在 1 秒到 7 天之间"}), 400
        scheduled_at = (datetime.now() + timedelta(seconds=countdown_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    if schedule_mode != "now":
        auto_print = 1  # 预约本质上是无障碍自动打印

    # ---- 兼容旧格式：单文件字段转为新格式数组 ----
    if files_input is None:
        file_id = data.get("file_id", "")
        file_name = data.get("file", "")
        copies = data.get("copies", 1)

        # 回填文件名
        if file_id and not file_name:
            conn = get_db()
            row = conn.execute("SELECT original_name FROM files WHERE id = ?", (file_id,)).fetchone()
            conn.close()
            if row:
                file_name = row["original_name"]

        if not file_name:
            return jsonify({"success": False, "message": "请提供 file 或 file_id 字段"}), 400

        files_input = [{"file_id": file_id or "", "file": file_name, "copies": copies}]

    # ---- 校验 files 数组 ----
    if not files_input or not isinstance(files_input, list):
        return jsonify({"success": False, "message": "files 字段必须是非空数组"}), 400

    user_is_admin = (g.user_role == "admin")
    # v24.1：管理员自行打印标记（仅管理员可设置；非管理员一律强制 0，防止伪造）
    admin_print = 1 if (user_is_admin and data.get("is_admin_print")) else 0
    owner_name = ""
    if admin_print:
        owner_name = (data.get("owner_name") or "").strip()
        if not owner_name:
            # 默认归属 = 提交者昵称（users.nickname），与收支清算成员名单对齐
            try:
                _uconn = get_user_db()
                _urow = _uconn.execute(
                    "SELECT nickname FROM users WHERE openid = ?", (g.openid,)
                ).fetchone()
                _uconn.close()
                if _urow and _urow["nickname"]:
                    owner_name = str(_urow["nickname"]).strip()
            except Exception:
                pass
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---- 幂等去重（P0-2）：同用户同 client_request_id 的订单 10 分钟内返回原单 ----
    # 原单已取消则不拦（用户可能在取消后重新提交）。
    if client_request_id:
        dup_conn = get_db()
        dup_row = dup_conn.execute(
            """SELECT id, order_number, status FROM orders
               WHERE openid = ? AND client_request_id = ? AND created_at > ?
               ORDER BY id DESC LIMIT 1""",
            (g.openid, client_request_id,
             (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
        dup_conn.close()
        if dup_row and dup_row["status"] != "canceled":
            # 构造与正常建单一致的响应（is_duplicate 标记供前端区分）
            return jsonify({
                "success": True,
                "is_duplicate": True,
                "message": "检测到重复提交，已返回原订单",
                "order_id": dup_row["id"],
                "order_number": dup_row["order_number"],
                "data": data,
            })

    order_number = generate_order_number()

    # ---- 目标设备（多设备接单）与打印机状态 ----
    # 2026-12：移动端提交时选择「可接单设备」作为目标；未指定/目标未启用接单 → 回退为任一接单设备接收
    target_client_id = (data.get("target_client_id") or "").strip()
    if target_client_id and not is_claiming(target_client_id):
        target_client_id = ""
    printer_online = is_printer_available()  # 有启用接单的设备（含离线：离线设备被指定时任务排队等其上线）

    # ---- 事务：插 1 条 orders + N 条 order_files ----
    conn = get_db()
    user_conn = get_user_db()
    try:
        # 先插父订单（聚合字段用首文件填充，后续严格通过 order_files 聚合）
        first_file_name = files_input[0].get("file", files_input[0].get("file_name", ""))
        conn.execute(
            """INSERT INTO orders (file_id, file, copies, status, created_at, openid, duplex,
                                   page_count, price_per_page, total_price, is_free,
                                   delivery_enabled, delivery_location, delivery_percentage,
                                   urgency, urgency_price, cover_page, cover_page_price, pickup_address,
                                   order_number, source, auto_print,
                                   schedule_mode, scheduled_at, schedule_frozen,
                                   client_request_id, owner_name, is_admin_print, remark, target_client)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, 1, 0, 0, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
            (files_input[0].get("file_id") or None, first_file_name, 0,
             created_at, g.openid, duplex,
             0,  # is_free 恒为 0 — 价格仅用于统计
             delivery_enabled, delivery_location, delivery_percentage,
             urgency, urgency_price, cover_page, cover_page_price, pickup_address,
             order_number, client, 1 if auto_print else 0,
             schedule_mode, scheduled_at,
             client_request_id, owner_name, admin_print, remark, target_client_id),
            # is_free 恒为 0 — 价格仅用于统计，无免费策略
        )
        order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 插每个子任务
        sub_tasks = []
        for f in files_input:
            f_id = f.get("file_id", "") or ""
            f_name = f.get("file", f.get("file_name", ""))
            # P1-4.4：份数钳制 1-999，非法类型 → 400（事务内抛异常回滚）
            try:
                f_copies = max(1, min(999, int(f.get("copies", 1))))
            except (ValueError, TypeError):
                raise OrderRejected("份数必须是 1-999 的整数")
            f_page_range = (f.get("page_range", "") or "").strip()

            # 计算页数与价格（优先从 files 表缓存读取）
            page_count = 1
            page_count_verified = False
            if f_id:
                frow = conn.execute(
                    "SELECT path, original_name, page_count, page_count_verified, openid FROM files WHERE id = ?",
                    (f_id,),
                ).fetchone()
                if frow:
                    # P2-10：文件归属校验（openid 为空 = 迁移前的旧记录，兼容放行）
                    if frow["openid"] and frow["openid"] != g.openid:
                        raise OrderRejected("文件归属校验失败，无权使用该文件")
                    cached = frow["page_count"] or 0
                    if cached > 0:
                        page_count = cached
                        page_count_verified = bool(frow["page_count_verified"])
                    elif frow["path"] and os.path.exists(frow["path"]):
                        # 缓存未命中，重新计算（PDF可当场获取，doc/docx返回0）
                        ext = os.path.splitext(frow["path"])[1].lower().lstrip(".")
                        page_count = get_file_page_count(frow["path"], ext) or 1
                        page_count_verified = ext not in ("doc", "docx")
                        conn.execute(
                            "UPDATE files SET page_count = ?, page_count_verified = ? WHERE id = ?",
                            (page_count, int(page_count_verified), f_id),
                        )
                    if not f_name:
                        f_name = frow["original_name"]
                else:
                    ext = os.path.splitext(f_name)[1].lower().lstrip(".")
                    page_count = get_file_page_count(None, ext) or 1
            else:
                ext = os.path.splitext(f_name)[1].lower().lstrip(".")
                page_count = get_file_page_count(None, ext) or 1

            if not f_name:
                f_name = "未知文件"

            # 读取每文件的双面设置（优先 files 数组中的值，其次顶层 duplex）
            f_duplex = f.get("duplex", duplex) or "on"

            is_free_val = 0  # 价格仅用于统计，无免费策略
            per_copy_price = calculate_price(page_count, f_duplex, f_page_range)
            total_price = round(per_copy_price * f_copies, 2)  # 始终计算真实价格，is_free 控制是否收费
            # 普通单从 queued 开始，由 push_print_task_to_client 原子锁定为 printing；
            # 预约单从 scheduled 开始，阶段①下发文件时为 downloading（不进 queued，避免被普通链路误分发）
            sub_status = "scheduled" if schedule_mode != "now" else "queued"
            reject_reason = ""

            # 若页数已确认，立即校验页码范围
            if page_count_verified and page_count > 0 and f_page_range:
                max_page = _parse_page_range_max(f_page_range)
                if max_page > page_count:
                    sub_status = "rejected"
                    reject_reason = f"页码范围超出文件总页数（填写 {max_page} 页，文件共 {page_count} 页）"
                    print(f"  [REJECT] 提交时打回: {f_name} range={f_page_range} 超出 {page_count} 页")

            f_image_orientation = f.get("image_orientation", "auto") or "auto"
            if f_image_orientation not in ("auto", "landscape", "portrait"):
                f_image_orientation = "auto"

            conn.execute(
                """INSERT INTO order_files (order_id, file_id, file_name, copies, page_count,
                                            page_range, price_per_page, total_price, is_free, status, created_at, duplex, reject_reason, image_orientation)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, f_id or None, f_name, f_copies, page_count,
                 f_page_range, 0, total_price, is_free_val, sub_status, created_at, f_duplex, reject_reason, f_image_orientation),
            )
            sub_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            sub_tasks.append({
                "id": sub_task_id, "file_id": f_id, "file_name": f_name,
                "copies": f_copies, "page_count": page_count,
                "page_count_verified": page_count_verified,
                "page_range": f_page_range,
                "total_price": total_price, "status": sub_status,
                "duplex": f_duplex,
                "image_orientation": f_image_orientation,
                "reject_reason": reject_reason,
            })

        # 汇总父订单 total_price 和 page_count
        parent_base_price = sum(st["total_price"] for st in sub_tasks)
        parent_page_count = sum(st["page_count"] * st["copies"] for st in sub_tasks)

        # 计算最终价格 = 基础打印费 + 附加服务费
        urgency_fee = urgency_price if urgency_price else 0
        cover_fee = round(cover_page_price * cover_page, 2) if cover_page else 0
        delivery_fee = round(parent_base_price * delivery_percentage / 100, 2) if delivery_percentage else 0
        parent_total_price = round(parent_base_price + urgency_fee + cover_fee + delivery_fee, 2)

        conn.execute(
            "UPDATE orders SET total_price = ?, page_count = ? WHERE id = ?",
            (parent_total_price, parent_page_count, order_id),
        )

        # 聚合初态并写入父订单
        new_status = aggregate_order_status(conn, order_id) or "queued"
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))

        # P2-4：临时授权原子消费（原"先查 temp_until 再清空"非原子，并发提交可重复消费）。
        # 改为条件 UPDATE（temp_until 仍存在且未过期才生效），影响行数为 0 → 拒绝本单并回滚；
        # 包在 db_lock 内，防止与兑换/其他提交交错。
        if not user_is_admin:
            urow = user_conn.execute(
                "SELECT temp_until FROM users WHERE openid = ?", (g.openid,),
            ).fetchone()
            if urow and urow["temp_until"]:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with db_lock:
                    cur2 = user_conn.execute(
                        "UPDATE users SET temp_until = NULL, updated_at = ? WHERE openid = ? AND temp_until IS NOT NULL AND temp_until > ?",
                        (now_str, g.openid, now_str),
                    )
                if cur2.rowcount == 0:
                    raise OrderRejected("临时授权已失效或被其他订单使用，请重新出示许可")
                user_conn.execute(
                    "UPDATE license_keys SET order_id = ? WHERE used_by = ? AND order_id IS NULL ORDER BY id DESC LIMIT 1",
                    (order_id, g.openid),
                )
                user_conn.commit()

        conn.commit()
    except OrderRejected as e:
        conn.rollback()
        try:
            user_conn.rollback()
        except Exception:
            pass
        conn.close()
        user_conn.close()
        return jsonify({"success": False, "message": e.message}), 400
    except Exception:
        conn.rollback()
        try:
            user_conn.rollback()
        except Exception:
            pass
        conn.close()
        user_conn.close()
        raise

    # ---- 推送（事务外，避免长事务） ----
    # 2026-12：多设备接单 —— 指定了目标设备则只发往该设备（离线则保持 queued，等其上线后由
    # process_pending_orders 推送）；未指定目标 → 任一在线接单设备。
    pushed_count = 0
    if target_client_id:
        client_id = target_client_id if target_client_id in get_active_clients() else None
    else:
        client_id = get_active_printer_client()
    if client_id:
        for st in sub_tasks:
            if st["file_id"]:
                if schedule_mode != "now":
                    # 预约单：阶段①先下发文件（downloading + 预约时间），本地到点再自动打印。
                    # 推送失败则保持 scheduled，等 process_scheduled_orders 扫描重试。
                    if push_print_task_to_client(st["id"], st["file_id"], st["file_name"],
                                                  st["copies"], st.get("duplex", duplex),
                                                  st.get("page_range", ""), client_id,
                                                  auto_print=True,
                                                  scheduled_download=True,
                                                  schedule_mode=schedule_mode,
                                                  scheduled_at=scheduled_at,
                                                  image_orientation=st.get("image_orientation", "auto")):
                        pushed_count += 1
                    continue
                if push_print_task_to_client(st["id"], st["file_id"], st["file_name"],
                                              st["copies"], st.get("duplex", duplex),
                                              st.get("page_range", ""), client_id,
                                              auto_print=bool(auto_print),
                                              image_orientation=st.get("image_orientation", "auto")):
                    pushed_count += 1
                else:
                    # 推送失败 → 降级子任务和父订单
                    conn = get_db()
                    conn.execute("UPDATE order_files SET status = 'queued' WHERE id = ?", (st["id"],))
                    st["status"] = "queued"
                    refresh_order_status(conn, order_id)
                    conn.commit()
                    conn.close()

    conn.close()

    # 重新读取最终聚合状态
    conn = get_db()
    final_status = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    final_status = final_status["status"] if final_status else "queued"

    print(f"收到任务 (order_id={order_id}): {len(sub_tasks)} 个文件, "
          f"status={final_status}, pushed={pushed_count}/{len(sub_tasks)}, "
          f"openid={g.openid[:8]}...")
    # P1-3.2：不再全量回显请求体（含派送地址等隐私），只打印关键摘要
    print(f"  [SUBMIT] openid={g.openid[:8]}..., files={len(sub_tasks)}, "
          f"delivery_enabled={delivery_enabled}, delivery_location={delivery_location!r}, "
          f"urgency={urgency!r}, cover_page={cover_page}, "
          f"schedule_mode={schedule_mode}, scheduled_at={scheduled_at}, "
          f"is_admin_print={admin_print}, owner_name={owner_name!r}")

    return jsonify({
        "success": True,
        "message": "任务已接收" + ("，已推送打印" if pushed_count > 0 else "，排队等待打印"),
        "order_id": order_id,
        "order_number": order_number,
        "status": final_status,
        "files": sub_tasks,
        "pushed_count": pushed_count,
        "data": data,
    })



@app.route("/api/pull_queued_orders", methods=["GET"])
def pull_queued_orders():
    """打印机客户端拉取排队中的子任务（原子取锁，每次返回一个任务，防止多打印机重复领取）
    2026-11：仅「启用接单」的设备可领取；未启用/无接管者时一律返回空，杜绝多设备同时接收。"""
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    client_id = request.args.get("client_id", "") or socket.gethostname()
    # 登记设备（HTTP 拉取路径的连接也纳入设备注册表，供收支清算「授权」页展示）
    register_device(client_id, request.args.get("device_name", "") or client_id)

    active_printer = get_active_printer_client()
    if not active_printer or client_id != active_printer:
        # 本机未接管打印机（或无接管设备）→ 不分配任何任务
        return jsonify({"success": True, "orders": [], "count": 0,
                        "take_orders": False, "active_client_id": active_printer or ""})

    task = fetch_and_lock_task(client_id)
    if not task:
        return jsonify({"success": True, "orders": [], "count": 0, "take_orders": True})

    # 每文件 duplex 已存入 order_files 表，直接从 task 读取
    duplex = task.get("duplex", "on") or "on"
    image_orientation = task.get("image_orientation", "auto") or "auto"

    # 构建与旧格式兼容的响应体（单元素数组）
    item = {
        "id": task["id"],
        "order_id": task["order_id"],
        "order_number": task.get("order_number", ""),
        "file_id": task["file_id"],
        "file": task["file_name"],           # 兼容旧客户端字段名
        "file_name": task["file_name"],
        "source_md5": task.get("source_md5", "") or "",
        "copies": task["copies"],
        "page_range": task.get("page_range", "") or "",
        "status": task["status"],
        "created_at": task["created_at"],
        "duplex": duplex,
        "image_orientation": image_orientation,
        "task_id": task["id"],               # 客户端用 task_id = order_files.id
        "options": {
            "copies": task["copies"],
            "duplex": duplex,
            "page_range": task.get("page_range", "") or "",
            "image_orientation": image_orientation,
        },
        "delivery_enabled": bool(task.get("delivery_enabled", False)),
        "delivery_location": task.get("delivery_location", "") or "",
        "urgency": task.get("urgency", "低") or "低",
        "cover_page": bool(task.get("cover_page", False)),
        "cover_page_price": float(task.get("cover_page_price", 0.10) or 0.10),
        "remark": task.get("remark", "") or "",
        "auto_print": bool(task.get("auto_print", False)),
        "owner_name": task.get("owner_name", "") or "",
        "is_admin_print": bool(task.get("is_admin_print", False)),
        # 2026-12：顾客订单标签页归属 = 下单用户绑定的成员名（与 push 通道一致）
        "bound_owner_name": _get_bound_owner_name(task.get("openid") or ""),
        # 订单来源（wechat/app/local），本地工具云端任务列表展示
        "source": task.get("source", "") or "wechat",
    }
    if task["file_id"]:
        item["download_url"] = make_download_url(task["file_id"])

    return jsonify({
        "success": True,
        "orders": [item],
        "count": 1,
    })


# ==================== 接单接管 / 设备授权（2026-11） ====================
# 多设备共连服务器时的唯一接管者机制：
#   · 仅「启用接单」的设备可接收订单（push / pull 均已门控到 active printer）；
#   · 启用接单时若已有其他在线设备接管 → 拒绝并告知接管者（计算机名 + 所有者）；
#   · 设备所有者绑定（计算机名 → 成员姓名）在收支清算「授权」页维护。


@app.route("/api/printer/claim", methods=["POST"])
def printer_claim():
    """启用接单（加入可接单设备集合，多设备可同时启用）。
    2026-12 重构：不再是「唯一接管者」独占，勾选接单的设备都会进入可接单设备列表，
    移动端提交订单时选择目标设备；任务只会发往目标设备（未指定目标时取任一在线接单设备）。"""
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id or len(client_id) > 64:
        return jsonify({"success": False, "message": "client_id 无效"}), 400

    device_name = (data.get("device_name") or "").strip()[:64]
    register_device(client_id, device_name)

    with _claim_lock:
        claim = _load_claim_impl()
        devices = claim.get("claiming_devices")
        if not isinstance(devices, dict):
            devices = {}
        devices[client_id] = {
            "claimed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "owner_name": get_device_owner(client_id),  # 以绑定所有者为准
        }
        claim["claiming_devices"] = devices
        _save_claim_impl(claim)
    broadcast_printer_state()
    print(f"[CLAIM] 设备 {client_id} 已启用接单（当前共 {len(devices)} 台可接单设备）")
    return jsonify({"success": True, "message": "接单已启用", "active": True,
                    "claiming_count": len(devices)})


@app.route("/api/printer/release", methods=["POST"])
def printer_release():
    """关闭接单（从可接单设备集合移除本机）。"""
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    with _claim_lock:
        claim = _load_claim_impl()
        devices = claim.get("claiming_devices")
        if isinstance(devices, dict) and client_id in devices:
            devices.pop(client_id, None)
            claim["claiming_devices"] = devices
            _save_claim_impl(claim)
            broadcast_printer_state()
            print(f"[CLAIM] 设备 {client_id} 已关闭接单（剩余 {len(devices)} 台可接单设备）")
            return jsonify({"success": True, "message": "接单已关闭"})
    return jsonify({"success": True, "message": "当前设备未启用接单"})


@app.route("/api/printer/devices", methods=["GET"])
def printer_devices():
    """返回连接过云服务器的全部设备：计算机名称、在线状态、所有者、是否接单。
    供收支清算「授权」页展示（打印机 token 鉴权，本地打印工具经 stats_server 代理调用）。"""
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    online_set = set(get_active_clients())
    devices = load_devices()
    result = []
    for cid, entry in devices.items():
        result.append({
            "client_id": cid,
            "device_name": entry.get("device_name", "") or cid,
            "online": cid in online_set,
            "owner_name": entry.get("owner_name", "") or "",
            "first_seen": entry.get("first_seen", ""),
            "last_seen": entry.get("last_seen", ""),
            "is_active": is_claiming(cid),
        })
    # 在线设备排前，其余按计算机名排序
    result.sort(key=lambda d: (not d["online"], d["device_name"].lower()))
    return jsonify({
        "success": True,
        "devices": result,
        "claiming_count": len(get_claiming_device_ids()),
    })


@app.route("/api/claiming_devices", methods=["GET"])
def claiming_devices():
    """返回全部启用接单的设备列表（含在线状态；离线设备也允许被选择发送任务，任务排队等其上线）。
    供移动端任务发起界面选择目标设备（公开接口，与 printer_status 一致）。"""
    online_set = set(get_active_clients())
    claiming = get_claiming_devices()
    result = []
    for cid, entry in claiming.items():
        dev = get_device_entry(cid)
        result.append({
            "client_id": cid,
            "device_name": dev.get("device_name", "") or cid,
            "owner_name": dev.get("owner_name", "") or "",
            "online": cid in online_set,
            "label": device_display_label(cid),
        })
    # 在线设备排前，其余按 client_id 排序
    result.sort(key=lambda d: (not d["online"], d["client_id"].lower()))
    return jsonify({"success": True, "devices": result})


@app.route("/api/printer/bind_owner", methods=["POST"])
def printer_bind_owner():
    """绑定设备所有者（计算机名 → 收支成员姓名）。"""
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id or len(client_id) > 64:
        return jsonify({"success": False, "message": "client_id 无效"}), 400
    owner_name = (data.get("owner_name") or "").strip()[:30]

    devices = load_devices()
    entry = devices.get(client_id) or {}
    entry["owner_name"] = owner_name
    devices[client_id] = entry
    save_devices(devices)
    # 该设备正是当前接管者 → 同步接单记录中的所有者名（claim 消息用）
    with _claim_lock:
        claim = _load_claim_impl()
        if claim.get("active_client_id") == client_id:
            claim["owner_name"] = owner_name
            _save_claim_impl(claim)
    # 2026-12：绑定后总是广播接单状态（含各设备自己的 owner_name），
    # 使本地工具无需重启即可立即用新所有者作为新建标签页的默认归属者
    broadcast_printer_state()
    print(f"[DEV] 设备 {client_id} 所有者绑定为「{owner_name or '未绑定'}」")
    return jsonify({"success": True, "message": "所有者已保存"})


@app.route("/api/printer/devices/delete", methods=["POST"])
def printer_devices_delete():
    """删除设备注册表中的一台设备（清理历史遗留 / 重复 client_id，如软件升级后产生的新 ID）。
    若删除的是当前「接单」设备则同时清空接管记录；在线设备下次连接会自动重新登记。"""
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id or len(client_id) > 64:
        return jsonify({"success": False, "message": "client_id 无效"}), 400

    devices = load_devices()
    existed = client_id in devices
    devices.pop(client_id, None)
    save_devices(devices)

    cleared_claim = False
    with _claim_lock:
        claim = _load_claim_impl()
        if claim.get("active_client_id") == client_id:
            claim.pop("active_client_id", None)
            claim.pop("owner_name", None)
            _save_claim_impl(claim)
            cleared_claim = True

    if not existed and not cleared_claim:
        return jsonify({"success": False, "message": "设备不存在"}), 404

    broadcast_printer_state()
    print(f"[DEV] 已删除设备 {client_id}（接单接管{'已清空' if cleared_claim else '未受影响'}）")
    return jsonify({"success": True, "message": "设备已删除"})


@app.route("/api/orders", methods=["GET"])
@login_required
def get_orders():
    """返回任务列表。默认仅返回当前用户自己的订单。
    管理员可通过 ?openid=xxx 查看指定授权用户的订单。
    超级管理员可通过 ?view=all 查看全部订单，或 ?openid=xxx 查看指定用户。
    支持分页: ?page=1&per_page=20
    """
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    page = max(1, page)
    per_page = max(1, min(100, per_page))
    offset = (page - 1) * per_page

    role = compute_role(g.openid)
    is_super = SUPER_ADMIN_OPENID and g.openid == SUPER_ADMIN_OPENID
    target_openid = (request.args.get("openid", "") or "").strip()
    view_all = is_super and request.args.get("view", "") == "all"
    source_filter = (request.args.get("source", "") or "").strip()

    conn = get_db()

    # 构建查询条件
    source_clause = ""
    source_params = []
    if source_filter in ("wechat", "app", "local"):
        source_clause = " AND source = ?"
        source_params = [source_filter]
    elif source_filter == "cloud":
        # 兼容旧版统计页：cloud 语义已并入 wechat（历史云端订单统一按小程序计）
        source_clause = " AND source = 'wechat'"
        source_params = []

    def _user_orders_clause(uid: str):
        """构造「某用户的任务列表」查询条件。

        默认按 openid=uid 过滤；若该 openid 在收支清算成员绑定表中绑定了成员，
        则额外并入本地打印工具创建的、归属该成员的本地订单
        （openid='local' AND source='local' AND owner_name=成员名）——
        使「历史授权用户 / 管理管理员 / 我的订单」的任务列表能看到名下本地打印的订单
        （v4.4 修复：此前本地订单 openid='local' 永远匹配不到具体用户）。
        带 source 过滤时保持原语义（只在指定来源内查询），不做并入。"""
        base = "openid = ?"
        p = [uid]
        if not source_filter:
            bound = _get_bound_owner_name(uid)
            if bound:
                base = "(openid = ? OR (source = 'local' AND owner_name = ?))"
                p = [uid, bound]
        return base + source_clause, p + source_params

    if view_all:
        # 超级管理员查看全部订单
        where_clause = "1 = 1" + source_clause
        params = source_params.copy()
    elif source_filter == "local" and (is_super or role == "admin"):
        # 管理员查看本地打印工具订单（openid='local'，不是微信 openid）
        where_clause = "openid = 'local'"
        params = []
    elif is_super and target_openid:
        # 超级管理员查看指定用户
        where_clause, params = _user_orders_clause(target_openid)
    elif role == "admin" and target_openid:
        # 管理员查看指定用户：需验证该用户是否由本管理员授权
        user_conn = get_user_db()
        auth_row = user_conn.execute(
            "SELECT used_by FROM license_keys WHERE created_by = ? AND used_by = ? LIMIT 1",
            (g.openid, target_openid),
        ).fetchone()
        user_conn.close()
        if not auth_row:
            conn.close()
            return jsonify({"success": False, "message": "无权查看该用户的订单"}), 403
        where_clause, params = _user_orders_clause(target_openid)
    else:
        # 默认：所有角色只看自己的订单（含归属自己绑定成员的本地订单）
        where_clause, params = _user_orders_clause(g.openid)

    # 查询总数
    total = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE {where_clause}",
        params,
    ).fetchone()[0]

    # 分页查询
    orders_rows = conn.execute(
        f"""
        SELECT id, file_id, file, copies, status, created_at, openid, duplex,
               page_count, is_free, total_price, order_number,
               delivery_enabled, delivery_location, delivery_percentage,
               urgency, urgency_price, cover_page, cover_page_price, pickup_address,
               schedule_mode, scheduled_at, schedule_frozen,
               source, owner_name, is_admin_print, remark, print_started_at,
               received_client, target_client
        FROM orders
        WHERE {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        params + [per_page, offset],
    ).fetchall()

    orders = []
    for o_row in orders_rows:
        order = dict(o_row)
        # 接单设备展示名（{所有者}的设备（{client_id}））；空值显示 — 
        order["received_label"] = device_display_label(order.get("received_client", ""))
        oid = order["id"]

        # 查询子任务
        of_rows = conn.execute(
            """SELECT id, file_id, file_name, copies, page_count, page_range,
                      page_range_original, page_range_truncated,
                      reject_reason,
                      total_price, is_free, status, created_at, duplex
               FROM order_files WHERE order_id = ? ORDER BY id ASC""",
            (oid,),
        ).fetchall()

        if of_rows:
            files = [dict(r) for r in of_rows]
            total_copies = sum(f["copies"] for f in files)
            total_pages = sum(f["page_count"] * f["copies"] for f in files)
            # 文件名摘要
            names = [f["file_name"] for f in files]
            if len(names) == 1:
                file_summary = names[0]
            else:
                file_summary = f"{names[0]} +{len(names) - 1} 个文件"
            # 用聚合状态覆盖父订单的旧状态
            order["status"] = aggregate_order_status(conn, oid) or order["status"]
        else:
            # 旧数据降级：没有 order_files → 用 orders 自身字段构造
            order_file = {
                "file_id": order.get("file_id"),
                "file_name": order.get("file", "未知文件"),
                "copies": order.get("copies", 1),
                "page_count": order.get("page_count", 1),
                "page_range": "",
                "total_price": order.get("total_price", 0),
                "is_free": order.get("is_free", 0),
                "status": order["status"],
                "duplex": order.get("duplex", "on"),
            }
            files = [order_file]
            total_copies = order.get("copies", 1)
            total_pages = order.get("page_count", 1) * total_copies
            names = [order["file"]]
            file_summary = order["file"]

        order["files"] = files
        order["total_copies"] = total_copies
        order["total_pages"] = total_pages
        order["file_summary"] = file_summary
        # 向前端保持一致：旧字段仍保留（兼容性），但语义标注为聚合值
        order["file"] = file_summary
        order["copies"] = total_copies
        # 等待时长（下单 → 开始打印）：后端用服务器时钟统一计算，前端不再自行相减
        order["wait_seconds"] = calc_wait_seconds(order.get("created_at"), order.get("print_started_at"))
        orders.append(order)

    # 关联临时许可密钥：每个订单显示它消费的密钥（license_keys.order_id 单向关联）
    license_map = {}
    order_ids = [o["id"] for o in orders]
    if order_ids:
        user_conn = get_user_db()
        try:
            placeholders = ",".join("?" for _ in order_ids)
            lrows = user_conn.execute(
                f"""SELECT order_id, key, type, created_at, used_at, expires_at, status
                    FROM license_keys
                    WHERE order_id IN ({placeholders})""",
                order_ids,
            ).fetchall()
            for lr in lrows:
                license_map[lr["order_id"]] = dict(lr)
        finally:
            user_conn.close()
    for o in orders:
        o["license_info"] = license_map.get(o["id"]) or None

    conn.close()
    return jsonify({
        "success": True,
        "orders": orders,
        "count": len(orders),
        "total": total,
        "page": page,
        "per_page": per_page,
        "has_more": (page * per_page) < total,
    })


@app.route("/api/order/<order_id>", methods=["GET"])
@login_required
def get_order_detail(order_id):
    """获取单个任务详情（仅限当前用户），含子任务文件列表"""
    conn = get_db()
    row = conn.execute(
        """
        SELECT o.id, o.file_id, o.file, o.copies,
               o.status, o.created_at, o.openid, o.duplex,
               o.page_count, o.is_free, o.total_price,
               o.delivery_enabled, o.delivery_location, o.delivery_percentage,
               o.urgency, o.urgency_price, o.cover_page, o.cover_page_price,
               o.pickup_address,
               o.schedule_mode, o.scheduled_at, o.schedule_frozen,
               o.owner_name, o.is_admin_print, o.remark, o.print_started_at,
               o.received_client, o.target_client
        FROM orders o
        WHERE o.id = ? AND o.openid = ?
        """,
        (order_id, g.openid),
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"success": False, "message": "任务不存在或无权访问"}), 404

    order = dict(row)
    # 接单设备展示名（{所有者}的设备（{client_id}））；空值显示 — 
    order["received_label"] = device_display_label(order.get("received_client", ""))

    # 查询子任务
    of_rows = conn.execute(
        """SELECT of.id, of.file_id, of.file_name, of.copies, of.page_count,
                  of.page_range, of.page_range_original, of.page_range_truncated,
                  of.total_price, of.is_free, of.status, of.duplex, of.image_orientation
           FROM order_files of WHERE of.order_id = ? ORDER BY of.id ASC""",
        (order_id,),
    ).fetchall()

    if of_rows:
        files = []
        for of_row in of_rows:
            f = dict(of_row)
            # 关联 files 表获取文件大小和类型
            if f["file_id"]:
                frow = conn.execute("SELECT size, original_name, saved_name FROM files WHERE id = ?",
                                    (f["file_id"],)).fetchone()
                if frow:
                    f["size"] = frow["size"]
                    f["original_name"] = frow["original_name"]
                    ext = os.path.splitext(frow["original_name"] or f["file_name"])[1]
                    f["file_type"] = ext.lstrip(".").upper() if ext else "未知"
                else:
                    f["size"] = 0
                    f["original_name"] = f["file_name"]
                    ext = os.path.splitext(f["file_name"])[1]
                    f["file_type"] = ext.lstrip(".").upper() if ext else "未知"
            else:
                f["size"] = 0
                f["original_name"] = f["file_name"]
                ext = os.path.splitext(f["file_name"])[1]
                f["file_type"] = ext.lstrip(".").upper() if ext else "未知"

            files.append(f)

        total_copies = sum(f["copies"] for f in files)
        total_pages = sum(f["page_count"] * f["copies"] for f in files)
        order["files"] = files
        order["total_copies"] = total_copies
        order["total_pages"] = total_pages
        # 用聚合状态
        order["status"] = aggregate_order_status(conn, order_id) or order["status"]
    else:
        # 旧数据降级
        conn2 = get_db()
        frow = conn2.execute("SELECT original_name, size, saved_name FROM files WHERE id = ?",
                             (order.get("file_id"),)).fetchone()
        conn2.close()
        original_name = frow["original_name"] if frow else order["file"]
        file_type = "未知"
        ext = os.path.splitext(original_name or order["file"])[1]
        file_type = ext.lstrip(".").upper() if ext else "未知"
        order["files"] = [{
            "id": None,
            "file_id": order.get("file_id"),
            "file_name": order.get("file", "未知文件"),
            "original_name": original_name,
            "copies": order.get("copies", 1),
            "page_count": order.get("page_count", 1),
            "total_price": order.get("total_price", 0),
            "is_free": order.get("is_free", 0),
            "status": order["status"],
            "page_range": "",
            "size": frow["size"] if frow else 0,
            "file_type": file_type,
            "duplex": order.get("duplex", "on"),
        }]
        order["total_copies"] = order.get("copies", 1)
        order["total_pages"] = order.get("page_count", 1) * order.get("copies", 1)

    # 关联临时许可密钥（每个订单消费的密钥）
    user_conn = get_user_db()
    try:
        lrow = user_conn.execute(
            "SELECT order_id, key, type, created_at, used_at, expires_at, status FROM license_keys WHERE order_id = ? LIMIT 1",
            (order_id,),
        ).fetchone()
        order["license_info"] = dict(lrow) if lrow else None
    finally:
        user_conn.close()
    order["wait_seconds"] = calc_wait_seconds(order.get("created_at"), order.get("print_started_at"))

    conn.close()
    return jsonify({"success": True, "order": order})


@app.route("/api/order_price/<order_id>", methods=["GET"])
@login_required
def get_order_price(order_id):
    """获取订单的价格明细（供结算/确认使用）。
    返回每份文件的价格明细和订单总价。
    """
    conn = get_db()
    row = conn.execute(
        "SELECT id, total_price, is_free FROM orders WHERE id = ? AND openid = ?",
        (order_id, g.openid),
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"success": False, "message": "任务不存在或无权访问"}), 404

    order = dict(row)

    # 查询子任务价格明细
    of_rows = conn.execute(
        """SELECT of.id, of.file_name, of.copies, of.page_count, of.duplex, of.page_range,
                  of.total_price, of.is_free, of.status
           FROM order_files of WHERE of.order_id = ? ORDER BY of.id ASC""",
        (order_id,),
    ).fetchall()

    files = []
    for of_row in of_rows:
        f = dict(of_row)
        # 附上单价明细（用于前端展示）
        per_copy_price = calculate_price(f["page_count"], f.get("duplex", "on"), f.get("page_range", ""))
        f["per_copy_price"] = per_copy_price
        f["unit"] = "元/张" if f.get("duplex") == "on" else "元/页"
        files.append(f)

    # 汇总
    total_files_price = sum(f.get("total_price", 0) for f in files)
    all_free = all(f.get("is_free", 0) for f in files)

    conn.close()
    return jsonify({
        "success": True,
        "order_id": int(order_id),
        "is_free": bool(all_free),
        "files": files,
        "total_price": total_files_price,
    })


@app.route("/api/cancel_order", methods=["POST"])
@login_required
def cancel_order():
    """取消任务（限 queued/printing/waiting/downloading/scheduled 状态且属于当前用户）。
    取消后通过 SocketIO 通知已连接的打印机客户端。

    P2-2：整个"读状态 → 校验 → 更新"纳入 db_lock（BEGIN IMMEDIATE），
    防止与 push/pull 并发时读到旧状态后覆盖对方写入。
    允许取消 printing 任务存在"打印机已开始打印"的小窗口：客户端收到
    order_canceled 事件后自行中止，可接受的窗口（现状即如此，保持并注明）。

    P2-9：允许取消预约单（scheduled/downloading/waiting）——用户预约了打印，
    到点前应能反悔；且取消时一次性把所有未终结子任务（含预约单的
    scheduled/downloading/waiting）置为 canceled，避免取消不完整导致
    process_scheduled_orders 把已取消订单的文件重新下发/打印。"""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    order_id = data.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400

    sub_tasks = []
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, status, openid FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()

            if not row:
                return jsonify({"success": False, "message": "任务不存在"}), 404

            if row["openid"] != g.openid:
                return jsonify({"success": False, "message": "无权操作此任务"}), 403

            if row["status"] not in ("queued", "printing", "waiting", "downloading", "scheduled"):
                return jsonify({"success": False, "message": f"任务状态为 {row['status']}，无法取消"}), 400

            # 查询被取消的子任务 ID 和已连接的打印机客户端。
            # 覆盖预约单的 scheduled/downloading/waiting（到点前也允许取消）
            sub_tasks = conn.execute(
                "SELECT id, operator_client FROM order_files WHERE order_id = ?"
                " AND status NOT IN ('sent', 'failed', 'canceled', 'rejected', 'abandoned')",
                (order_id,),
            ).fetchall()

            # 取消父订单和所有未终结子任务（同时清空 locked_at）。
            # 一次性覆盖 queued/printing/waiting/downloading/scheduled 等全部非终态，
            # 避免取消不完整导致已取消订单被 process_scheduled_orders 重新下发/卡死。
            conn.execute(
                "UPDATE order_files SET status = 'canceled', locked_at = '' WHERE order_id = ?"
                " AND status NOT IN ('sent', 'failed', 'canceled', 'rejected', 'abandoned')",
                (order_id,),
            )
            conn.execute(
                "UPDATE orders SET status = 'canceled' WHERE id = ?",
                (order_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # 通过 SocketIO 通知已连接的打印机客户端
    task_ids = [t["id"] for t in sub_tasks]
    notified_clients = set()
    for t in sub_tasks:
        client_id = t["operator_client"]
        if client_id and client_id not in notified_clients:
            notified_clients.add(client_id)
            with printer_clients_lock:
                info = printer_clients.get(client_id)
                sid = info["sid"] if info else None
            if sid:
                try:
                    socketio.emit("order_canceled", {
                        "order_id": order_id,
                        "task_ids": task_ids,
                    }, to=sid)
                    print(f"  [CANCEL] 已通知打印机 {client_id}: 订单 #{order_id} 已取消")
                except Exception as e:
                    print(f"  [CANCEL] 通知打印机失败: {e}")

    return jsonify({"success": True, "message": "任务已取消"})


@app.route("/api/accept_order", methods=["POST"])
def accept_order():
    """打印机确认接受订单（需 token 认证）。将状态从 printing 改为 accepted（终端状态）。

    P2-9：订单已被用户取消 → 拒绝接受（409）。原先父订单被无条件翻成 accepted，
    已取消订单在窗口期被"添加"会把 canceled 污染成 accepted，且本地会打印已取消订单。
    改为 db_lock 原子校验 + 仅当确有子任务被接受时才更新父订单。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "") or request.args.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400
    # P1-8：accept 时刷新 locked_at（复用 P0-1 的 locked_at 列）——
    # check_printing_timeout 对已 accept 任务按 accept 时刻起算 3 分钟反馈超时
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            o = conn.execute(
                "SELECT status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not o:
                return jsonify({"success": False, "message": "任务不存在"}), 404
            if o["status"] == "canceled":
                print(f"  [SKIP] accept 订单 #{order_id}: 已被用户取消，拒绝接受")
                return jsonify({"success": False, "message": "订单已被取消，无法接受"}), 409
            cur = conn.execute(
                "UPDATE order_files SET status = 'accepted', locked_at = ? WHERE order_id = ? AND status = 'printing'",
                (now_str, order_id),
            )
            if cur.rowcount > 0:
                conn.execute(
                    "UPDATE orders SET status = 'accepted' WHERE id = ?",
                    (order_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    print(f"  [ACCEPT] 订单 #{order_id} 已被打印机接受")
    return jsonify({"success": True, "message": "订单已接受"})


@app.route("/api/reject_order", methods=["POST"])
def reject_order():
    """打印机打回订单（需 token 认证）。将订单状态设为 rejected。

    P2-9：已取消订单不允许打回（409），避免 canceled 被覆盖成 rejected；
    且仅当确有子任务被打回时才更新父订单（防空转污染）。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "") or request.args.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400

    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            o = conn.execute(
                "SELECT status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not o:
                return jsonify({"success": False, "message": "任务不存在"}), 404
            if o["status"] == "canceled":
                print(f"  [SKIP] reject 订单 #{order_id}: 已被用户取消，拒绝打回")
                return jsonify({"success": False, "message": "订单已被取消，无法打回"}), 409
            # 将子任务和父订单都设为 rejected
            cur = conn.execute(
                "UPDATE order_files SET status = 'rejected' WHERE order_id = ? AND status IN ('queued', 'printing')",
                (order_id,),
            )
            if cur.rowcount > 0:
                conn.execute(
                    "UPDATE orders SET status = 'rejected' WHERE id = ?",
                    (order_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    print(f"  [REJECT] 订单 #{order_id} 已被打印机打回")
    return jsonify({"success": True, "message": "订单已打回"})


# ==================== 收支清算配置（云端存储） ====================


@app.route("/api/finance/config", methods=["GET"])
def get_finance_config():
    """读取收支清算云端配置（需 printer token 认证）。data 为 null 表示尚未保存。"""
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    conn = get_db()
    try:
        row = conn.execute("SELECT data FROM finance_config WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row and row["data"]:
        try:
            return jsonify({"success": True, "data": json.loads(row["data"])})
        except Exception:
            return jsonify({"success": False, "message": "配置数据损坏"}), 500
    return jsonify({"success": True, "data": None})


@app.route("/api/finance/config", methods=["POST"])
def save_finance_config():
    """保存收支清算云端配置（需 printer token 认证）。body: {"data": {...完整配置}}"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    payload = data.get("data", None)
    if payload is None:
        return jsonify({"success": False, "message": "缺少 data"}), 400
    if isinstance(payload, dict):
        payload.pop("_filepath", None)  # 清理内部字段
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO finance_config (id, data, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (json.dumps(payload, ensure_ascii=False),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return jsonify({"success": True, "message": "配置已保存"})


@app.route("/api/abandon_order", methods=["POST"])
def abandon_order():
    """打印机放弃已接受的订单（需 token 认证）。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "") or request.args.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400
    conn = get_db()
    # 放弃非 sent 子任务：queued/printing/accepted/offline_unknown/waiting/downloading 常规状态，
    # 以及 failed（任务可能已被自动超时判败，但用户显式放弃应覆盖为 abandoned）；
    # 仅 sent（已打印完成）不触碰——避免误伤已完成打印的订单
    conn.execute(
        "UPDATE order_files SET status = 'abandoned' WHERE order_id = ?"
        " AND status IN ('queued', 'printing', 'accepted', 'offline_unknown',"
        "                'waiting', 'downloading', 'failed')",
        (order_id,),
    )
    conn.commit()
    # 用聚合状态重算父订单（而非无条件覆盖为 abandoned）：
    # 全部子任务 sent → 父订单保持 sent；被放弃的文件 → 父订单变为 abandoned
    refresh_order_status(conn, order_id)
    conn.commit()
    conn.close()
    print(f"  [ABANDON] 订单 #{order_id} 放弃完成（非终态子任务→abandoned，父订单按聚合重算）")
    return jsonify({"success": True, "message": "订单已标记为放弃打印"})


@app.route("/api/abandon_reserved_order", methods=["POST"])
def abandon_reserved_order():
    """本地打印工具放弃未提交的预留订单（按 order_number，需 token 认证）。
    场景：用户在本地工具中点击"复制"获得了订单号但未点击"开始打印"，
    直接关闭窗口或删除标签页时调用，将 reserved 标记为 abandoned，
    同时补记价格（本地工具在获取订单号时已知文件/份数/双面配置）。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    order_number = data.get("order_number", "") or request.args.get("order_number", "")
    if not order_number:
        return jsonify({"success": False, "message": "缺少 order_number"}), 400
    total_price = data.get("total_price", None)
    conn = get_db()
    try:
        if total_price is not None:
            # 已配置打印（工具放弃时已知文件/份数/双面 → 有金额）：保留为 abandoned 订单，
            # 并补记归属（本地单口径：默认占位名 / 管理员自行打印），避免留下无归属记录
            cursor = conn.execute(
                """UPDATE orders SET status = 'abandoned', total_price = ?,
                                     owner_name = COALESCE(NULLIF(owner_name, ''), ?),
                                     is_admin_print = CASE WHEN is_admin_print = 0 THEN 1 ELSE is_admin_print END
                   WHERE order_number = ? AND status = 'reserved'""",
                (float(total_price), DEFAULT_OWNER_NAME, order_number),
            )
            price_info = f"，价格 ¥{float(total_price):.2f}"
        else:
            # 未配置的纯占位单（直接关闭/删除标签页）：无金额无归属，直接删除整行，不留残留
            cursor = conn.execute(
                """DELETE FROM orders WHERE order_number = ? AND status = 'reserved'
                   AND file = '（预留位置）' AND total_price <= 0
                   AND NOT EXISTS (SELECT 1 FROM order_files f WHERE f.order_id = orders.id)""",
                (order_number,),
            )
            price_info = ""
        count = cursor.rowcount
        if count > 0:
            conn.commit()
            action = "清除（纯占位）" if total_price is None else "放弃打印" + price_info
            print(f"  [ABANDON] 预留订单 {order_number} 已被{action}")
        if total_price is not None:
            # 预留单的子任务同步置 abandoned（无条件：父订单可能已被放弃，残留 reserved
            # 子任务会让结算/列表的实时聚合把 abandoned 回退成 reserved）
            conn.execute(
                "UPDATE order_files SET status = 'abandoned'"
                " WHERE order_id IN (SELECT id FROM orders WHERE order_number = ?) AND status = 'reserved'",
                (order_number,),
            )
            conn.commit()
        return jsonify({"success": True, "message": f"已处理 {count} 个预留订单"})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


# ==================== 本地订单上报（本地打印工具使用）====================

@app.route("/api/local_orders", methods=["POST"])
def local_orders():
    """本地打印工具上报本地打印任务。需 token 认证。

    v4.6：支持预留上报——本地工具点击「复制价格」后先以 status='reserved' 上报完整
    订单信息（文件明细 + 备注 + 价格），使"已预留"订单在后端非空占位；打印完成再以
    status='sent' 覆盖（或断线重连按本地是否已打印选择状态）。两次上报都会删旧插新
    覆盖文件明细与备注，保证后端信息与本地一致。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    data = request.get_json(silent=True) or {}
    order_number = data.get("order_number", "")
    files = data.get("files", [])
    total_price = data.get("total_price", 0)
    created_at = data.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    # 订单归属标记（本地工具订单号右侧下拉 + 勾选框）
    owner_name = (data.get("owner_name", "") or "").strip()
    is_admin_print = data.get("is_admin_print")
    if is_admin_print is not None:
        is_admin_print = 1 if is_admin_print else 0
    # 上报状态：'sent'（打印完成）/ 'reserved'（预留占位，复制价格后未打印）
    status = data.get("status", "sent")
    if status not in ("sent", "reserved"):
        status = "sent"
    # 订单备注（≤100 字）
    remark = str(data.get("remark", "") or "")[:100]
    # 接收设备（多设备接单）：本地工具上报本机 client_id，用于记录订单「接单设备」
    report_client_id = (data.get("client_id") or "").strip()

    if not order_number or not files:
        return jsonify({"success": False, "message": "缺少 order_number 或 files"}), 400

    conn = get_db()
    try:
        # 查询是否已存在占位记录（由 /api/next_order_number 创建，或离线同步重复提交）
        existing = conn.execute(
            "SELECT id, status FROM orders WHERE order_number = ?", (order_number,)
        ).fetchone()

        order_file_label = f"本地打印 {len(files)} 个文件"

        if existing:
            order_id = existing["id"]
            old_status = existing["status"]
            # 如果已经是 sent/accepted 等终态 → 幂等跳过（预留上报不覆盖已完成的订单）
            if old_status in ("sent", "accepted"):
                conn.close()
                return jsonify({"success": True, "message": "订单已存在，跳过同步", "order_id": order_id})

            # 更新占位记录为正式订单（reserved 占位 → reserved 完整 或 sent），并覆盖备注
            conn.execute(
                """UPDATE orders SET file = ?, total_price = ?, status = ?,
                                     created_at = ?, source = 'local', remark = ?
                   WHERE id = ?""",
                (order_file_label, total_price, status, created_at, remark, order_id),
            )
            # 归属标记：本地工具上报时随订单写入（缺失则默认占位名/管理员自行打印，
            # 与历史无标记订单的处理保持一致）
            conn.execute(
                "UPDATE orders SET owner_name = ?, is_admin_print = ? WHERE id = ?",
                (owner_name or DEFAULT_OWNER_NAME, is_admin_print if is_admin_print is not None else 1, order_id),
            )
            # 清除旧子任务（如果有），重新插入
            conn.execute("DELETE FROM order_files WHERE order_id = ?", (order_id,))
        else:
            # 新插入（离线同步或直接调用场景）
            conn.execute(
                """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                                       total_price, source, owner_name, is_admin_print, remark)
                   VALUES (?, 1, ?, ?, 'local', ?, ?, 'local', ?, ?, ?)""",
                (order_file_label, status, created_at, order_number, total_price,
                 owner_name or DEFAULT_OWNER_NAME, is_admin_print if is_admin_print is not None else 1,
                 remark),
            )
            order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 本地订单的开始打印时间（print_started_at）：sent 上报发生在打印启动那一刻，
        # 由服务器时钟写入（与 created_at 同源，等待时长才可信）。幂等——仅首次写入，
        # 重打/重复上报不覆盖；reserved 预留上报（复制价格时刻）不写。
        if status == "sent":
            conn.execute(
                """UPDATE orders SET print_started_at =
                       COALESCE(NULLIF(print_started_at, ''), ?)
                   WHERE id = ?""",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id),
            )
            # 记录接收设备（首次写入，幂等；本地直接打印的订单归属本机）
            if report_client_id:
                conn.execute(
                    "UPDATE orders SET received_client = COALESCE(NULLIF(received_client, ''), ?) WHERE id = ?",
                    (report_client_id, order_id),
                )

        # 子任务状态：预留单用 'reserved'（不进 queued 推送链路，聚合也不污染为 sent），
        # 打印完成上报用 'sent'
        sub_status = "reserved" if status == "reserved" else "sent"
        for f_info in files:
            conn.execute(
                """INSERT INTO order_files (order_id, file_name, copies, page_count,
                                            total_price, status, duplex, created_at, page_range)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, f_info.get("file_name", ""), f_info.get("copies", 1),
                 f_info.get("page_count", 0), f_info.get("cost", 0),
                 sub_status, f_info.get("duplex", "on"), created_at, f_info.get("page_range", "")),
            )
        conn.commit()
        return jsonify({"success": True, "order_id": order_id})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


# ==================== 用户身份接口 ====================


@app.route("/api/me", methods=["GET"])
@login_required
def get_me():
    """返回当前用户的 openid、角色、临时授权信息（含许可密钥详情）"""
    role = compute_role(g.openid)
    temp_until = None
    has_temp_access = False
    license_info = None
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    row = conn.execute(
        "SELECT temp_until, role, theme_mode FROM users WHERE openid = ?", (g.openid,)
    ).fetchone()
    if row and row["temp_until"]:
        temp_until = row["temp_until"]
        has_temp_access = row["temp_until"] > now_str
    # 查询关联的许可密钥：temp 用户或通过许可密钥升级的 admin
    need_license = has_temp_access or (
        row and row["role"] == "admin" and not is_admin(g.openid)
    )
    if need_license:
        lk_row = conn.execute(
            """SELECT lk.key, lk.created_by, lk.expires_at, lk.type,
                      lk.created_at, lk.used_at, lk.validity_minutes,
                      u.nickname AS creator_nickname
               FROM license_keys lk
               LEFT JOIN users u ON lk.created_by = u.openid
               WHERE lk.used_by = ?
               ORDER BY lk.id DESC LIMIT 1""",
            (g.openid,),
        ).fetchone()
        if lk_row:
            # 当前角色是管理员（永久授权）或密钥本身是 admin 类型 → 视为永久；
            # 只有临时许可才按到期时间判断是否过期。
            is_permanent = (role == "admin") or (lk_row["type"] == "admin")
            license_info = {
                "key": lk_row["key"],
                "type": lk_row["type"] or "temp",
                "creator_openid": lk_row["created_by"],
                "creator_nickname": lk_row["creator_nickname"] or "",
                "created_at": lk_row["created_at"],
                "used_at": lk_row["used_at"],
                "expires_at": lk_row["expires_at"],
                "validity_minutes": lk_row["validity_minutes"],
                "expired": False if is_permanent else not (
                    lk_row["expires_at"] and lk_row["expires_at"] > now_str
                ),
            }
    conn.close()
    is_super = SUPER_ADMIN_OPENID and g.openid == SUPER_ADMIN_OPENID
    theme_mode = (row["theme_mode"] if row and row["theme_mode"] else "auto") if row else "auto"
    return jsonify({
        "success": True,
        "openid": g.openid,
        "is_admin": role == "admin",
        "is_super_admin": bool(is_super),
        "role": role,
        "temp_until": temp_until,
        "has_temp_access": has_temp_access,
        "license_info": license_info,
        "theme_mode": theme_mode,
    })


# ==================== 主题偏好同步 ====================


@app.route("/api/me/theme", methods=["PUT"])
@login_required
def update_theme():
    """保存用户主题偏好：'auto' | 'light' | 'dark'"""
    data = request.get_json()
    theme_mode = (data or {}).get("theme_mode", "auto")
    if theme_mode not in ("auto", "light", "dark"):
        return jsonify({"success": False, "message": "无效的主题模式"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    existing = conn.execute("SELECT openid FROM users WHERE openid = ?", (g.openid,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE users SET theme_mode = ?, updated_at = ? WHERE openid = ?",
            (theme_mode, now, g.openid),
        )
    else:
        conn.execute(
            "INSERT INTO users (openid, theme_mode, updated_at) VALUES (?, ?, ?)",
            (g.openid, theme_mode, now),
        )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "theme_mode": theme_mode})


# ==================== 授权用户列表（管理员查看）====================


@app.route("/api/authorized_users", methods=["GET"])
@login_required
def authorized_users():
    """管理员/超级管理员查看自己的“历史授权用户”：所有被本管理员授权过的用户
    （管理员 + 临时用户，含已移除/已过期），每个用户附全部密钥记录（支持多次授权）。
    """
    role = compute_role(g.openid)
    if role != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    rows = conn.execute(
        """SELECT DISTINCT lk.used_by AS openid
           FROM license_keys lk
           WHERE lk.created_by = ? AND lk.used_by IS NOT NULL""",
        (g.openid,),
    ).fetchall()
    user_openids = [r["openid"] for r in rows]

    # 从 orders.db 批量查询每个用户的最近订单和订单数
    order_stats = {}
    if user_openids:
        orders_conn = get_db()
        try:
            # 用 IN 一次查询所有用户的最近订单时间
            placeholders = ",".join("?" for _ in user_openids)
            last_rows = orders_conn.execute(
                f"""SELECT openid, MAX(created_at) AS last_order, COUNT(*) AS order_count
                    FROM orders
                    WHERE openid IN ({placeholders})
                    GROUP BY openid""",
                user_openids,
            ).fetchall()
            for lr in last_rows:
                order_stats[lr["openid"]] = {
                    "last_order": lr["last_order"] or "",
                    "order_count": lr["order_count"] or 0,
                }
            # v4.4：并入归属本地订单 —— 用户绑定收支成员后，该成员名下的本地打印订单
            # （source='local', owner_name=成员名）计入该用户的关联订单数与最近订单时间，
            # 与任务列表 /api/orders?openid= 的归并口径保持一致。
            bound_map = {}  # openid → 绑定的成员名
            for oid in user_openids:
                b = _get_bound_owner_name(oid)
                if b:
                    bound_map[oid] = b
            if bound_map:
                names = sorted(set(bound_map.values()))
                ph = ",".join("?" for _ in names)
                local_rows = orders_conn.execute(
                    f"""SELECT owner_name, MAX(created_at) AS last_order, COUNT(*) AS order_count
                        FROM orders
                        WHERE source = 'local' AND owner_name IN ({ph})
                        GROUP BY owner_name""",
                    names,
                ).fetchall()
                local_by_name = {r["owner_name"]: r for r in local_rows}
                for oid, bname in bound_map.items():
                    lr = local_by_name.get(bname)
                    if not lr:
                        continue
                    st = order_stats.setdefault(oid, {"last_order": "", "order_count": 0})
                    st["order_count"] += lr["order_count"] or 0
                    lo = lr["last_order"] or ""
                    if lo and (not st["last_order"] or lo > st["last_order"]):
                        st["last_order"] = lo
        finally:
            orders_conn.close()

    users = []
    for r in rows:
        openid = r["openid"]
        urow = conn.execute(
            "SELECT nickname, avatar_path, role, temp_until, removed_at, removed_by FROM users WHERE openid = ?",
            (openid,),
        ).fetchone()
        key_rows = conn.execute(
            """SELECT key, type, created_at, used_at, expires_at, validity_minutes, order_id, status
               FROM license_keys
               WHERE created_by = ? AND used_by = ?
               ORDER BY id DESC""",
            (g.openid, openid),
        ).fetchall()

        cur_role = compute_role(openid)
        removed = bool(urow and urow["removed_at"])
        if removed:
            status = "removed"
        elif cur_role == "admin":
            status = "permanent"
        elif urow and urow["temp_until"] and urow["temp_until"] > now_str:
            status = "active"
        else:
            status = "expired"

        types = sorted({(k["type"] or "temp") for k in key_rows})
        license_type = "admin" if "admin" in types else ("temp" if "temp" in types else "")
        license_type_label = "both" if len(types) > 1 else license_type

        records = [{
            "key": k["key"],
            "type": k["type"] or "temp",
            "created_at": k["created_at"],
            "used_at": k["used_at"],
            "expires_at": k["expires_at"],
            "validity_minutes": k["validity_minutes"],
            "order_id": k["order_id"],
            "status": k["status"],
        } for k in key_rows]
        latest_key = records[0] if records else None

        stats = order_stats.get(openid, {})
        users.append({
            "openid": openid,
            "openid_short": openid[:8] + "..." if openid else "",
            "nickname": ((urow["nickname"] if urow and urow["nickname"] else "") or "未知用户"),
            "avatar_url": get_avatar_url(openid, (urow["avatar_path"] if urow else "") or ""),
            "role": cur_role,
            "is_super": bool(SUPER_ADMIN_OPENID and openid == SUPER_ADMIN_OPENID),
            "removed": removed,
            "removed_at": (urow["removed_at"] if urow else None),
            "removed_by": (urow["removed_by"] if urow else None),
            "status": status,
            "license_type": license_type_label,
            "latest_key": latest_key,
            "records": records,
            "last_order": stats.get("last_order", ""),
            "order_count": stats.get("order_count", 0),
        })
    conn.close()
    # 按最近使用密钥时间降序排列
    users.sort(key=lambda u: (u["latest_key"] or {}).get("used_at") or "", reverse=True)

    return jsonify({"success": True, "users": users, "count": len(users)})


@app.route("/api/admin/temp_users", methods=["GET"])
@login_required
def admin_temp_users():
    """“已临时授权的普通用户”：本管理员创建的临时密钥所授权、且未被移除的普通用户。
    卡片仅用于展示 + 左滑移除（无跳转）。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    rows = conn.execute(
        """SELECT DISTINCT lk.used_by AS openid
           FROM license_keys lk
           WHERE lk.created_by = ? AND lk.type = 'temp' AND lk.used_by IS NOT NULL""",
        (g.openid,),
    ).fetchall()

    users = []
    for r in rows:
        openid = r["openid"]
        urow = conn.execute(
            "SELECT nickname, avatar_path, temp_until, removed_at, role FROM users WHERE openid = ?",
            (openid,),
        ).fetchone()
        if not urow or urow["removed_at"]:
            continue
        if compute_role(openid) == "admin":
            continue  # 已是管理员 → 显示在“管理管理员”，不在此模块
        lrow = conn.execute(
            """SELECT key, type, created_at, used_at, expires_at, order_id, status
               FROM license_keys
               WHERE used_by = ? AND type = 'temp'
               ORDER BY id DESC LIMIT 1""",
            (openid,),
        ).fetchone()
        active = bool(urow["temp_until"] and urow["temp_until"] > now_str)
        users.append({
            "openid": openid,
            "openid_short": openid[:8] + "..." if openid else "",
            "nickname": (urow["nickname"] or "") or "未知用户",
            "avatar_url": get_avatar_url(openid, urow["avatar_path"] or ""),
            "status": "active" if active else "expired",
            "temp_until": urow["temp_until"],
            "license_key": (lrow["key"] if lrow else ""),
            "license_used_at": (lrow["used_at"] if lrow else ""),
            "license_expires_at": (lrow["expires_at"] if lrow else ""),
            "order_id": (lrow["order_id"] if lrow else None),
        })
    conn.close()
    users.sort(key=lambda u: u["license_used_at"] or "", reverse=True)
    return jsonify({"success": True, "users": users, "count": len(users)})


@app.route("/api/admin/remove_user", methods=["POST"])
@login_required
def admin_remove_user():
    """管理员/超级管理员手动移除“已临时授权的普通用户”：
    清除其临时授权并记录移除信息（removed_at/removed_by），密钥记录保留。
    普通管理员只能移除自己授权过的用户；超级管理员可移除任意用户。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    data = request.get_json() or {}
    target_openid = (data.get("openid", "") or "").strip()
    if not target_openid:
        return jsonify({"success": False, "message": "缺少 openid 参数"}), 400

    if not (SUPER_ADMIN_OPENID and g.openid == SUPER_ADMIN_OPENID):
        conn = get_user_db()
        try:
            licensed = conn.execute(
                "SELECT 1 FROM license_keys WHERE created_by = ? AND used_by = ? AND type = 'temp' LIMIT 1",
                (g.openid, target_openid),
            ).fetchone()
        finally:
            conn.close()
        if not licensed:
            return jsonify({"success": False, "message": "无权移除该用户"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    try:
        conn.execute(
            "UPDATE users SET temp_until = NULL, removed_at = ?, removed_by = ?, updated_at = ? WHERE openid = ?",
            (now_str, g.openid, now_str, target_openid),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"管理员 {g.openid[:8]}... 移除了临时用户 {target_openid[:8]}...")
    return jsonify({"success": True, "message": "已移除该用户"})


@app.route("/api/admin/user_detail", methods=["GET"])
@login_required
def admin_user_detail():
    """管理员/超级管理员查看指定用户的卡片信息（头像、昵称、角色、许可密钥详情）。
    超级管理员可查任意用户；普通管理员只能查看自己授权过的用户（保护隐私）。
    license_info 与 /api/me 一致：取该用户最近一条已使用的密钥（不限是否过期），
    并附带创建时间/使用时间/有效期/是否过期等详细字段。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    openid = (request.args.get("openid", "") or "").strip()
    if not openid:
        return jsonify({"success": False, "message": "缺少 openid 参数"}), 400

    # 普通管理员只能查看自己创建密钥授权过的用户
    if not (SUPER_ADMIN_OPENID and g.openid == SUPER_ADMIN_OPENID):
        conn = get_user_db()
        try:
            licensed = conn.execute(
                "SELECT 1 FROM license_keys WHERE created_by = ? AND used_by = ? LIMIT 1",
                (g.openid, openid),
            ).fetchone()
        finally:
            conn.close()
        if not licensed:
            return jsonify({"success": False, "message": "无权查看该用户"}), 403

    conn = get_user_db()
    try:
        urow = conn.execute(
            "SELECT nickname, avatar_path FROM users WHERE openid = ?", (openid,)
        ).fetchone()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lk_row = conn.execute(
            """SELECT lk.key, lk.type, lk.created_at, lk.used_at, lk.expires_at,
                      lk.validity_minutes, lk.created_by,
                      u2.nickname AS creator_nickname
               FROM license_keys lk
               LEFT JOIN users u2 ON lk.created_by = u2.openid
               WHERE lk.used_by = ?
               ORDER BY lk.id DESC LIMIT 1""",
            (openid,),
        ).fetchone()
    finally:
        conn.close()

    target_role = compute_role(openid)
    license_info = None
    if lk_row:
        # 用户当前是管理员（永久授权）或密钥本身是 admin 类型 → 视为永久；
        # 只有临时许可才按到期时间判断是否过期。
        is_permanent = (target_role == "admin") or (lk_row["type"] == "admin")
        license_info = {
            "key": lk_row["key"],
            "type": lk_row["type"] or "temp",
            "creator_openid": lk_row["created_by"],
            "creator_nickname": lk_row["creator_nickname"] or "",
            "created_at": lk_row["created_at"],
            "used_at": lk_row["used_at"],
            "expires_at": lk_row["expires_at"],
            "validity_minutes": lk_row["validity_minutes"],
            "expired": False if is_permanent else not (
                lk_row["expires_at"] and lk_row["expires_at"] > now_str
            ),
        }

    return jsonify({
        "success": True,
        "openid": openid,
        "nickname": (urow["nickname"] if urow and urow["nickname"] else "") or "微信用户",
        "avatar_url": get_avatar_url(openid, (urow["avatar_path"] if urow else "") or ""),
        "role": target_role,
        "is_super": bool(SUPER_ADMIN_OPENID and openid == SUPER_ADMIN_OPENID),
        "license_info": license_info,
    })


# ==================== 用户资料接口 ====================


@app.route("/api/profile", methods=["GET"])
@login_required
def get_profile():
    """获取当前用户的头像和昵称"""
    conn = get_user_db()
    row = conn.execute(
        "SELECT nickname, avatar_path FROM users WHERE openid = ?",
        (g.openid,),
    ).fetchone()
    conn.close()

    if row:
        nickname = row["nickname"] or ""
        avatar_url = get_avatar_url(g.openid, row["avatar_path"])
        return jsonify({
            "success": True,
            "nickname": nickname,
            "avatar_url": avatar_url,
        })
    else:
        return jsonify({
            "success": True,
            "nickname": "",
            "avatar_url": "",
        })


@app.route("/api/profile", methods=["POST"])
@login_required
def update_profile():
    """更新用户昵称和头像（支持 JSON 或 multipart）"""
    nickname = ""
    avatar_file = None

    if request.is_json:
        data = request.get_json()
        nickname = (data or {}).get("nickname", "")
    else:
        nickname = request.form.get("nickname", "")
        avatar_file = request.files.get("avatar")

    avatar_path = None
    if avatar_file and avatar_file.filename:
        # P0-2：大小校验（≤ 2MB），超限拒绝，避免头像填盘
        avatar_file.seek(0, 2)
        avatar_size = avatar_file.tell()
        avatar_file.seek(0)
        if avatar_size > _AVATAR_MAX_SIZE:
            return jsonify({"success": False, "message": "头像文件不能超过 2MB"}), 400

        # P0-2：魔数校验（jpeg/png/webp/gif），非图片文件直接拒绝
        if not _is_valid_avatar(avatar_file.stream):
            return jsonify({"success": False, "message": "头像文件格式不支持（仅支持 jpg/png/webp/gif）"}), 400

        ext = os.path.splitext(avatar_file.filename)[1] or ".jpg"
        saved_name = f"{g.openid}{ext}"
        file_path = os.path.join(AVATAR_DIR, saved_name)
        avatar_file.save(file_path)
        avatar_path = compress_avatar(file_path)
        if not avatar_path:
            # P0-2：PIL 打不开（损坏/解压炸弹等）→ 删除已保存文件并拒绝，不再原样保留
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError:
                pass
            return jsonify({"success": False, "message": "头像文件无法处理，请更换图片"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_user_db()
    existing = conn.execute("SELECT openid FROM users WHERE openid = ?", (g.openid,)).fetchone()

    if existing:
        if avatar_path:
            conn.execute(
                "UPDATE users SET nickname = ?, avatar_path = ?, updated_at = ? WHERE openid = ?",
                (nickname, avatar_path, now, g.openid),
            )
        else:
            conn.execute(
                "UPDATE users SET nickname = ?, updated_at = ? WHERE openid = ?",
                (nickname, now, g.openid),
            )
    else:
        conn.execute(
            "INSERT INTO users (openid, nickname, avatar_path, updated_at) VALUES (?, ?, ?, ?)",
            (g.openid, nickname, avatar_path or "", now),
        )
    conn.commit()

    # 查询最终状态，正确返回 avatar_url
    final = conn.execute(
        "SELECT nickname, avatar_path FROM users WHERE openid = ?", (g.openid,)
    ).fetchone()
    conn.close()

    final_avatar = final["avatar_path"] if final else ""
    avatar_url = get_avatar_url(g.openid, final_avatar)

    return jsonify({
        "success": True,
        "nickname": nickname or (final["nickname"] if final else ""),
        "avatar_url": avatar_url,
    })


@app.route("/api/avatar")
def get_avatar():
    """获取用户头像（无需登录，通过 openid 查询）"""
    openid = request.args.get("openid", "")
    if not openid:
        return jsonify({"success": False, "message": "缺少 openid"}), 400

    conn = get_user_db()
    row = conn.execute(
        "SELECT avatar_path FROM users WHERE openid = ?",
        (openid,),
    ).fetchone()
    conn.close()

    if row and row["avatar_path"] and os.path.exists(row["avatar_path"]):
        resp = send_file(row["avatar_path"], mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    # 返回默认头像
    default = os.path.join(os.path.dirname(__file__), "static", "default-avatar.png")
    if os.path.exists(default):
        return send_file(default, mimetype="image/png")
    return jsonify({"success": False, "message": "无头像"}), 404


# ==================== 许可密钥接口 ====================


@app.route("/api/license/create", methods=["POST"])
@login_required
def license_create():
    """管理员创建一次性限时许可密钥（1-10 分钟有效）。
    支持 type 参数: 'temp'（临时许可，默认）或 'admin'（永久管理员权限）。
    admin 类型仅超级管理员可创建。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    data = request.get_json() or {}
    key_type = (data.get("type", "temp") or "temp").strip().lower()
    if key_type not in ("temp", "admin"):
        return jsonify({"success": False, "message": "type 只能为 temp 或 admin"}), 400

    # admin 类型密钥仅超级管理员可创建
    if key_type == "admin":
        if not SUPER_ADMIN_OPENID or g.openid != SUPER_ADMIN_OPENID:
            return jsonify({"success": False, "message": "仅超级管理员可创建 admin 类型密钥"}), 403

    import secrets
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_user_db()
    # 生成唯一密钥（全量查重 + 数据库 UNIQUE 约束双保险，避免任何碰撞）
    for _ in range(20):
        license_key = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        dup = conn.execute(
            "SELECT id FROM license_keys WHERE key = ?",
            (license_key,),
        ).fetchone()
        if not dup:
            break

    # admin 和 temp 都使用 1-10 分钟倒计时（admin 被兑换后变永久，temp 兑换后临时授权）
    validity = int(data.get("validity_minutes", 5))
    validity = max(1, min(10, validity))
    expires = now + timedelta(minutes=validity)
    conn.execute(
        """INSERT INTO license_keys (key, created_by, validity_minutes, created_at, expires_at, type, status)
           VALUES (?, ?, ?, ?, ?, ?, 'unused')""",
        (license_key, g.openid, validity,
         now.strftime("%Y-%m-%d %H:%M:%S"),
         expires.strftime("%Y-%m-%d %H:%M:%S"),
         key_type),
    )
    conn.commit()
    conn.close()

    print(f"管理员 {g.openid[:8]}... 创建{key_type}许可密钥: {license_key}, 有效期 {validity} 分钟")
    return jsonify({
        "success": True,
        "key": license_key,
        "type": key_type,
        "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"),
        "validity_minutes": validity,
    })


@app.route("/api/license/redeem", methods=["POST"])
@login_required
def license_redeem():
    """用户兑换许可密钥。
    - temp 类型: 设置 users.temp_until = expires_at（临时打印权限）
    - admin 类型: 设置 users.role = 'admin'（永久管理员）
    """
    # 限速（P1-6 / 防滥用）：每用户每分钟兑换次数上限（管理员可调，单 worker 内存计数）
    if not _rate_limit(f"license_redeem:{g.openid}", _SECURITY["redeem_rate_limit"], 60):
        return jsonify({"success": False, "message": "兑换过于频繁，请稍后再试"}), 429

    data = request.get_json() or {}
    raw_key = (data.get("key", "") or "").strip().upper()
    if len(raw_key) != 8:
        return jsonify({"success": False, "message": "密钥格式不正确"}), 400

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_user_db()
    # 原子条件 UPDATE：仅当未使用且未过期才生效；兑换后标记 status='used'（记录永久保留）
    conn.execute(
        """UPDATE license_keys SET used_by = ?, used_at = ?, status = 'used'
           WHERE key = ? AND used_by IS NULL AND expires_at > ?""",
        (g.openid, now_str, raw_key, now_str),
    )
    conn.commit()

    if conn.total_changes == 0:
        # 密钥不存在、已使用或已过期
        row = conn.execute("SELECT used_by, expires_at FROM license_keys WHERE key = ?", (raw_key,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "message": "密钥不存在"}), 404
        if row["used_by"] is not None:
            return jsonify({"success": False, "message": "密钥已被使用"}), 400
        return jsonify({"success": False, "message": "密钥已过期"}), 400

    # 读取密钥类型和过期时间
    key_row = conn.execute(
        "SELECT type, expires_at FROM license_keys WHERE key = ?", (raw_key,)
    ).fetchone()
    key_type = key_row["type"] if key_row else "temp"
    expires_at = key_row["expires_at"] if key_row else now_str

    # 根据密钥类型处理用户权限
    existing = conn.execute("SELECT openid FROM users WHERE openid = ?", (g.openid,)).fetchone()
    if key_type == "admin":
        # admin 类型: 设为管理员，清除临时授权；同时清除历史移除标记（重新授权）
        if existing:
            conn.execute(
                "UPDATE users SET role = 'admin', temp_until = NULL, removed_at = NULL, removed_by = NULL, updated_at = ? WHERE openid = ?",
                (now_str, g.openid),
            )
        else:
            conn.execute(
                "INSERT INTO users (openid, role, temp_until, nickname, avatar_path, updated_at) VALUES (?, 'admin', NULL, '', '', ?)",
                (g.openid, now_str),
            )
    else:
        # temp 类型: 设置临时授权截止时间；同时清除历史移除标记（重新授权）
        if existing:
            conn.execute(
                "UPDATE users SET temp_until = ?, removed_at = NULL, removed_by = NULL, updated_at = ? WHERE openid = ?",
                (expires_at, now_str, g.openid),
            )
        else:
            conn.execute(
                "INSERT INTO users (openid, role, temp_until, nickname, avatar_path, updated_at) VALUES (?, 'guest', ?, '', '', ?)",
                (g.openid, expires_at, now_str),
            )
    conn.commit()
    conn.close()

    print(f"用户 {g.openid[:8]}... 成功兑换{key_type}许可密钥: {raw_key}")
    return jsonify({"success": True, "message": "许可验证成功，您已获得打印权限"})


@app.route("/api/license/active", methods=["GET"])
@login_required
def license_active():
    """查询当前管理员所有未过期的许可密钥（支持同时创建多个密钥）。
    每个密钥状态: unused（未兑换）/ used_waiting（已兑换等待提交任务）/ used_done（已提交任务）
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    try:
        rows = conn.execute(
            """SELECT id, key, type, used_by, order_id, validity_minutes, created_at, expires_at, status
               FROM license_keys
               WHERE created_by = ? AND expires_at > ?
                 AND (status IS NULL OR status NOT IN ('revoked', 'archived', 'finished'))
               ORDER BY id DESC""",
            (g.openid, now_str),
        ).fetchall()

        if not rows:
            return jsonify({"success": True, "active": False, "keys": []})

        keys = []
        # 收集所有 used_by 用户，批量查昵称头像（避免 N+1）
        used_by_set = set()
        for row in rows:
            ub = row["used_by"]
            if ub:
                used_by_set.add(ub)
        user_info = {}
        if used_by_set:
            for ub in used_by_set:
                urow = conn.execute(
                    "SELECT nickname, avatar_path FROM users WHERE openid = ?", (ub,)
                ).fetchone()
                if urow:
                    user_info[ub] = {
                        "nickname": urow["nickname"] or "微信用户",
                        "avatar_url": get_avatar_url(ub, urow["avatar_path"] or ""),
                    }
                else:
                    user_info[ub] = {"nickname": "微信用户", "avatar_url": ""}

        # 批量查订单状态（orders.db）
        all_used = [row["used_by"] for row in rows if row["used_by"]]
        order_map = {}
        if all_used:
            orders_conn = get_db()
            try:
                # 查各用户最近订单
                for ub in set(all_used):
                    orow = orders_conn.execute(
                        "SELECT id, status, total_price FROM orders WHERE openid = ? ORDER BY id DESC LIMIT 1",
                        (ub,),
                    ).fetchone()
                    if orow:
                        order_map[ub] = {
                            "order_id": orow["id"],
                            "order_status": orow["status"],
                            "order_total_price": orow["total_price"] or 0,
                        }
            finally:
                orders_conn.close()

        for row in rows:
            r = dict(row)
            used_by = r.get("used_by") or None
            status = "unused"
            order_info = {}
            if used_by:
                oi = order_map.get(used_by)
                # 优先用 license_keys.order_id 关联的订单
                license_oid = r.get("order_id")
                if license_oid and oi and oi.get("order_id") == license_oid:
                    order_info = oi
                    status = "used_done"
                elif oi:
                    order_info = oi
                    status = "used_done"
                else:
                    status = "used_waiting"

            ui = user_info.get(used_by, {})
            keys.append({
                "key": r["key"],
                "type": r.get("type") or "temp",
                "status": status,
                "expires_at": r["expires_at"],
                "validity_minutes": r["validity_minutes"],
                "created_at": r["created_at"],
                "used_by": used_by,
                "used_by_nickname": ui.get("nickname", ""),
                "used_by_avatar_url": ui.get("avatar_url", ""),
                "order_id": order_info.get("order_id"),
                "order_status": order_info.get("order_status"),
                "order_total_price": order_info.get("order_total_price"),
            })

        return jsonify({"success": True, "active": len(keys) > 0, "keys": keys})
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[ERROR] /api/license/active 查询失败: {e}")
        return jsonify({"success": False, "message": "服务器内部错误"}), 500
    finally:
        conn.close()


@app.route("/api/license/revoke", methods=["POST"])
@login_required
def license_revoke():
    """管理员作废/归档指定的许可密钥（行保留，不影响已获得的授权）：
    - 未使用密钥 → status='revoked'（作废，他人无法再使用）
    - 已使用密钥 → status='archived'（从“活跃密钥”列表移除卡片，授权记录保留在历史授权用户中）
    body: { "key": "ABCD1234" }
    不传 key 则处理该管理员全部密钥（兼容旧版）。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    data = request.get_json() or {}
    target_key = (data.get("key", "") or "").strip().upper()

    conn = get_user_db()
    # 软处理，行永久保留：未使用 → revoked（作废）；已使用 → archived（归档，移出活跃列表）。
    if target_key:
        conn.execute(
            """UPDATE license_keys
               SET status = CASE WHEN used_by IS NULL THEN 'revoked' ELSE 'archived' END
               WHERE key = ? AND created_by = ?""",
            (target_key, g.openid),
        )
    else:
        # 兼容旧版：处理该管理员全部密钥（未使用作废，已使用归档）
        conn.execute(
            """UPDATE license_keys
               SET status = CASE WHEN used_by IS NULL THEN 'revoked' ELSE 'archived' END
               WHERE created_by = ?""",
            (g.openid,),
        )
    conn.commit()
    conn.close()

    print(f"管理员 {g.openid[:8]}... 作废/归档许可密钥: {target_key or '全部'}")
    return jsonify({"success": True})


@app.route("/api/license/finish", methods=["POST"])
@login_required
def license_finish():
    """管理员结束打印任务：查询许可证关联的订单价格详情，标记许可证为已完成。
    body: { "key": "ABCD1234" }
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    data = request.get_json() or {}
    raw_key = (data.get("key", "") or "").strip().upper()

    if len(raw_key) != 8:
        return jsonify({"success": False, "message": "密钥格式不正确"}), 400

    conn = get_user_db()
    lrow = conn.execute(
        "SELECT id, key, used_by, order_id FROM license_keys WHERE key = ? AND created_by = ?",
        (raw_key, g.openid),
    ).fetchone()

    if not lrow:
        conn.close()
        return jsonify({"success": False, "message": "密钥不存在或不属于您"}), 404

    order_id = lrow["order_id"]
    price_detail = None

    if order_id:
        # 查询订单价格明细（orders / order_files 在 orders.db）
        orders_conn = get_db()
        try:
            orow = orders_conn.execute(
                "SELECT id, total_price, is_free FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if orow:
                order = dict(orow)
                of_rows = orders_conn.execute(
                    """SELECT of.id, of.file_name, of.copies, of.page_count, of.duplex, of.page_range,
                              of.total_price, of.is_free, of.status
                       FROM order_files of WHERE of.order_id = ? ORDER BY of.id ASC""",
                    (order_id,),
                ).fetchall()
                files = []
                for of_row in of_rows:
                    f = dict(of_row)
                    f["per_copy_price"] = calculate_price(f["page_count"], f.get("duplex", "on"), f.get("page_range", ""))
                    f["unit"] = "元/张" if f.get("duplex") == "on" else "元/页"
                    files.append(f)
                price_detail = {
                    "order_id": order["id"],
                    "is_free": bool(order.get("is_free", 0)),
                    "total_price": sum(f.get("total_price", 0) for f in files),
                    "files": files,
                }
        finally:
            orders_conn.close()

    # 标记为已完成（status → finished，行保留作为授权记录，绝不硬删）
    conn.execute(
        "UPDATE license_keys SET status = 'finished' WHERE id = ?",
        (lrow["id"],),
    )
    conn.commit()
    conn.close()

    print(f"管理员 {g.openid[:8]}... 结束了密钥 {raw_key} 的打印任务")
    return jsonify({
        "success": True,
        "message": "任务已结束",
        "price_detail": price_detail,
    })


def cleanup_expired_license_keys():
    """清理已过期且未使用的许可密钥 / 个人认证密钥（超过 1 小时）"""
    cutoff = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    conn.execute("DELETE FROM license_keys WHERE used_by IS NULL AND expires_at < ?", (cutoff,))
    conn.execute("DELETE FROM bind_keys WHERE status = 'unused' AND expires_at < ?", (cutoff,))
    conn.commit()
    conn.close()


@app.route("/api/admin/users", methods=["GET"])
@login_required
def admin_users_list():
    """管理员查看所有普通用户列表（含头像、昵称、许可时间）"""
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    conn = get_user_db()
    rows = conn.execute(
        """
        SELECT u.openid, u.nickname, u.avatar_path,
               (SELECT lk.key FROM license_keys lk
                WHERE lk.used_by = u.openid
                ORDER BY lk.used_at DESC LIMIT 1) as license_key,
               (SELECT lk.used_at FROM license_keys lk
                WHERE lk.used_by = u.openid
                ORDER BY lk.used_at DESC LIMIT 1) as licensed_at
        FROM users u
        WHERE u.role = 'user'
        ORDER BY licensed_at DESC
        """
    ).fetchall()
    conn.close()

    users = []
    for row in rows:
        entry = dict(row)
        entry["nickname"] = entry.get("nickname") or "微信用户"
        entry["avatar_url"] = get_avatar_url(entry["openid"], entry.get("avatar_path") or "")
        entry["licence_key"] = entry.get("license_key") or ""
        entry["licensed_at"] = entry.get("licensed_at") or ""
        users.append(entry)

    return jsonify({
        "success": True,
        "users": users,
        "count": len(users),
    })


# ==================== 管理员：管理员列表/移除 ====================


@app.route("/api/admin/admins", methods=["GET"])
@login_required
def admin_admins_list():
    """超级管理员查看所有管理员列表（含昵称、头像、openid）。
    支持可选分页参数: page（页码，1 起始）、page_size（每页条数，默认 20）。
    """
    if not SUPER_ADMIN_OPENID or g.openid != SUPER_ADMIN_OPENID:
        return jsonify({"success": False, "message": "仅限超级管理员操作"}), 403

    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size

    conn = get_user_db()
    # 统计总数
    total = conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin'"
    ).fetchone()[0]

    rows = conn.execute(
        """SELECT openid, nickname, avatar_path, updated_at
           FROM users WHERE role = 'admin'
           ORDER BY updated_at DESC
           LIMIT ? OFFSET ?""",
        (page_size, offset),
    ).fetchall()
    conn.close()

    admins = []
    for row in rows:
        entry = dict(row)
        entry["nickname"] = entry.get("nickname") or "微信用户"
        entry["avatar_url"] = get_avatar_url(entry["openid"], entry.get("avatar_path") or "")
        entry["is_super"] = (entry["openid"] == SUPER_ADMIN_OPENID)
        admins.append(entry)

    return jsonify({
        "success": True,
        "admins": admins,
        "count": len(admins),
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@app.route("/api/admin/remove_admin", methods=["POST"])
@login_required
def admin_remove_admin():
    """超级管理员移除某个管理员（将其 role 改为 guest，清除 temp_until，并记录移除信息）。
    不能移除超级管理员自己。
    被移除的管理员会出现在超级管理员的“历史授权用户”中（状态=已移除）。
    """
    if not SUPER_ADMIN_OPENID or g.openid != SUPER_ADMIN_OPENID:
        return jsonify({"success": False, "message": "仅限超级管理员操作"}), 403

    data = request.get_json() or {}
    target_openid = (data.get("openid", "") or "").strip()

    if not target_openid:
        return jsonify({"success": False, "message": "缺少 openid 参数"}), 400

    if target_openid == g.openid:
        return jsonify({"success": False, "message": "不能移除超级管理员自己"}), 400

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    existing = conn.execute(
        "SELECT role FROM users WHERE openid = ? AND role = 'admin'",
        (target_openid,),
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"success": False, "message": "用户不是管理员或不存在"}), 404

    conn.execute(
        "UPDATE users SET role = 'guest', temp_until = NULL, removed_at = ?, removed_by = ?, updated_at = ? WHERE openid = ?",
        (now_str, g.openid, now_str, target_openid),
    )
    # 若该管理员没有任何 admin 类型密钥记录（历史硬删导致）→ 补一条归档记录，
    # 保证“移除的管理员必进历史授权用户、授权记录不凭空消失”。
    has_admin_key = conn.execute(
        "SELECT 1 FROM license_keys WHERE used_by = ? AND type = 'admin' LIMIT 1",
        (target_openid,),
    ).fetchone()
    if not has_admin_key:
        import uuid
        archive_key = 'ARCHIVED' + uuid.uuid4().hex[:6].upper()
        conn.execute(
            """INSERT INTO license_keys
               (key, created_by, used_by, validity_minutes, created_at, expires_at, used_at, type, status)
               VALUES (?, ?, ?, 0, ?, ?, ?, 'admin', 'archived')""",
            (archive_key, g.openid, target_openid, now_str, now_str, now_str),
        )
    conn.commit()
    conn.close()

    print(f"超级管理员 {g.openid[:8]}... 移除了管理员 {target_openid[:8]}...")
    return jsonify({"success": True, "message": "已移除该管理员权限"})


# ==================== 管理员：存储统计 ====================


def _format_size(size_bytes):
    """将字节数格式化为人类可读的字符串"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


@app.route("/api/admin/storage", methods=["GET", "POST", "DELETE"])
@login_required
def admin_storage():
    """管理员查看/设置服务器缓存文件统计与保留时间"""
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "需要管理员权限"}), 403

    # ---- DELETE: 删除全部缓存文件 ----
    if request.method == "DELETE":
        deleted_count = 0
        deleted_size = 0

        md5_index = load_md5_index()

        for root, dirs, files in os.walk(UPLOAD_DIR):
            # 跳过 avatars 子树（用户头像）
            if os.path.basename(root) == "avatars":
                dirs[:] = []  # 阻止继续递归子目录
                continue
            for fname in files:
                # 跳过配置文件
                if fname in ("md5_index.json", "retention_config.json", "security_config.json"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    size = os.path.getsize(fpath)
                    os.remove(fpath)
                    deleted_count += 1
                    deleted_size += size
                except OSError as e:
                    print(f"  [DELETE-ALL] 删除失败 {fpath}: {e}")

        # 清空 MD5 索引
        save_md5_index({})

        # 清空 files 表中的路径引用
        conn = get_db()
        conn.execute("UPDATE files SET path = '', size = 0")
        conn.commit()
        conn.close()

        # 同步通知所有在线本地打印工具清空 PDF 缓存
        _notify_clients("clear_local_cache", {
            "message": f"管理员清空了服务器缓存 ({deleted_count} 个文件)",
        })

        print(f"  [DELETE-ALL] 已删除 {deleted_count} 个文件, 释放 {_format_size(deleted_size)}")
        return jsonify({
            "success": True,
            "message": f"已删除 {deleted_count} 个文件",
            "deleted_count": deleted_count,
            "deleted_size_display": _format_size(deleted_size),
        })

    # ---- POST: 设置保留时间 ----
    if request.method == "POST":
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

        days = data.get("retention_days", None)
        hours = data.get("retention_hours", None)

        if days is None or hours is None:
            return jsonify({"success": False, "message": "请提供 retention_days 和 retention_hours"}), 400

        try:
            days = int(days)
            hours = int(hours)
        except (ValueError, TypeError):
            return jsonify({"success": False, "message": "天数/小时数必须为整数"}), 400

        if days < 0 or days > 365:
            return jsonify({"success": False, "message": "天数范围: 0-365"}), 400
        if hours < 0 or hours > 23:
            return jsonify({"success": False, "message": "小时数范围: 0-23"}), 400

        cfg = {"days": days, "hours": hours}
        save_retention_config(cfg)

        # 保存后立即执行一次清理
        cleanup_expired_files()

        # 同步通知所有在线本地打印工具更新缓存保留时间
        _notify_clients("storage_config_updated", {
            "retention_days": days,
            "retention_hours": hours,
        })

        return jsonify({"success": True, "message": "保留时间已更新"})

    # ---- GET: 查看存储统计 + 保留时间 ----
    total_files = 0
    total_size = 0

    for root, dirs, files in os.walk(UPLOAD_DIR):
        # 跳过 avatars 子树（用户头像不参与缓存统计）
        if os.path.basename(root) == "avatars":
            dirs[:] = []  # 阻止继续递归
            continue
        for fname in files:
            # 跳过配置文件
            if fname in ("md5_index.json", "retention_config.json", "order_counter.json", "security_config.json"):
                continue
            fpath = os.path.join(root, fname)
            try:
                stat = os.stat(fpath)
                total_files += 1
                total_size += stat.st_size
            except OSError:
                pass

    cfg = load_retention_config()

    return jsonify({
        "success": True,
        "total_files": total_files,
        "total_size": total_size,
        "total_size_display": _format_size(total_size),
        "retention_days": cfg["days"],
        "retention_hours": cfg["hours"],
    })


@app.route("/api/admin/security", methods=["GET", "POST"])
@login_required
def admin_security():
    """超管/管理员查看、调整防滥用（DDoS 防护）阈值。
    与 /api/admin/storage 同款模式：GET 返回当前阈值，POST 校验并保存到 security_config.json，
    保存后内存副本即时生效（限速/配额/磁盘守卫/排队超时均读取 _SECURITY）。"""
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "需要管理员权限"}), 403

    if request.method == "POST":
        data = request.get_json() or {}
        cfg = dict(_SECURITY)
        for key, (lo, hi) in SECURITY_RANGES.items():
            if key not in data or data[key] is None:
                continue
            try:
                v = int(data[key])
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": f"{key} 必须为整数"}), 400
            if not (lo <= v <= hi):
                return jsonify({"success": False, "message": f"{key} 范围: {lo}-{hi}"}), 400
            cfg[key] = v
        save_security_config(cfg)
        return jsonify({"success": True, "message": "防滥用阈值已更新"})

    # GET：返回当前阈值
    cfg = dict(_SECURITY)
    return jsonify({"success": True, **cfg})


# ==================== 统计与报表接口 ====================


@app.route("/api/statistics/my", methods=["GET"])
@login_required
def statistics_my():
    """当前登录用户查看自己的月度打印统计（基于 order_files 聚合）"""
    year = request.args.get("year", str(datetime.now().year))
    month = request.args.get("month", str(datetime.now().month))

    conn = get_db()
    row = conn.execute(
        """
        SELECT SUM(COALESCE(of_count.pages, o.page_count * o.copies)) AS total_pages,
               COUNT(DISTINCT o.id) AS total_orders
        FROM orders o
        LEFT JOIN (
            SELECT order_id,
                   SUM(page_count * copies) AS pages
            FROM order_files
            WHERE status != 'canceled'
            GROUP BY order_id
        ) of_count ON o.id = of_count.order_id
        WHERE o.openid = ?
          AND strftime('%Y', o.created_at) = ?
          AND strftime('%m', o.created_at) = ?
          AND o.status != 'canceled'
        """,
        (g.openid, year, month.zfill(2)),
    ).fetchone()
    conn.close()

    total_pages = row["total_pages"] or 0
    total_orders = row["total_orders"] or 0

    return jsonify({
        "success": True,
        "year": int(year),
        "month": int(month),
        "stats": {
            "total_pages": total_pages,
            "total_orders": total_orders,
        },
    })


@app.route("/api/admin/statistics/revenue", methods=["GET"])
def admin_statistics_revenue():
    """管理员收益统计接口（printer token 鉴权）。
    查询指定日期区间内的所有订单收益，按来源/用户/月份分组。
    参数:
        start_date  - 起始日期 (YYYY-MM-DD)，必填
        end_date    - 结束日期 (YYYY-MM-DD)，必填
        status      - 订单状态筛选，逗号分隔，默认不含 canceled/rejected/abandoned/reserved
        token       - 打印机认证 token
    金额口径: 已入账 = sent 子任务合计；自打订单（is_admin_print=1）不计入收益金额，
              订单/文件/页数等运营量仍计入。
    """
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    if not start_date or not end_date:
        return jsonify({"success": False, "message": "缺少 start_date 或 end_date 参数"}), 400

    # 状态筛选（排除已取消/已拒绝/已放弃/已预留）
    status_param = request.args.get("status", "").strip()
    if status_param:
        allowed_statuses = [s.strip() for s in status_param.split(",") if s.strip()]
    else:
        allowed_statuses = ["sent", "printing", "queued", "failed", "offline_unknown"]

    conn = get_db()
    # 构建状态占位符
    status_placeholders = ",".join("?" for _ in allowed_statuses)
    params = [start_date + " 00:00:00", end_date + " 23:59:59"] + allowed_statuses

    # ── 汇总 / 按用户分组在订单明细构建后由 Python 统一聚合 ──
    # 金额口径 = 文件费（按子任务状态归入已入账/应收未收）+ 订单级附加服务费
    # （派送/加急/首页费，按订单有效文件状态归入）。自打订单金额不计。
    revenue_summary = {"total": 0.0,
                       "wechat": 0.0, "app": 0.0, "local": 0.0,
                       "wechat_orders": 0, "app_orders": 0, "local_orders": 0,
                       "wechat_files": 0, "app_files": 0, "local_files": 0,
                       "total_pages": 0,
                       "wechat_pages": 0, "app_pages": 0, "local_pages": 0}
    by_user = []

    # ── 订单明细（时间倒序） ──
    # v25 重构：LEFT JOIN 保留无子任务的订单（如本地放弃单），子任务状态条件移入 ON；
    #          补充附加服务/自动打印/预约/地址等字段供收支清算云端页展示。
    order_rows = conn.execute(
        f"""SELECT o.id, o.order_number, o.source, o.openid, o.status,
                   o.total_price, o.created_at, o.delivery_enabled,
                   o.delivery_location, o.urgency, o.cover_page,
                   o.owner_name, o.is_admin_print,
                   o.auto_print, o.schedule_mode, o.scheduled_at,
                   o.delivery_percentage, o.urgency_price, o.cover_page_price,
                   o.pickup_address, o.remark, o.print_started_at,
                   o.received_client, o.target_client
            FROM orders o
            LEFT JOIN order_files of ON o.id = of.order_id AND of.status IN ({status_placeholders})
            WHERE o.created_at >= ? AND o.created_at <= ?
              AND o.status IN ({status_placeholders})
            GROUP BY o.id
            ORDER BY o.created_at DESC""",
        allowed_statuses + [start_date + " 00:00:00", end_date + " 23:59:59"] + allowed_statuses,
    ).fetchall()

    # 收集所有订单 ID 用于批量查询子文件
    order_ids = [r["id"] for r in order_rows]
    files_by_order = {}
    if order_ids:
        of_placeholders = ",".join("?" for _ in order_ids)
        of_rows = conn.execute(
            f"""SELECT id, order_id, file_name, copies, page_count, duplex,
                       page_range, total_price, status, created_at, image_orientation, is_free,
                       reject_reason
                FROM order_files
                WHERE order_id IN ({of_placeholders})
                ORDER BY order_id, id""",
            order_ids,
        ).fetchall()
        for of_row in of_rows:
            oid = of_row["order_id"]
            if oid not in files_by_order:
                files_by_order[oid] = []
            files_by_order[oid].append({
                "id": of_row["id"],
                "file_name": of_row["file_name"],
                "copies": of_row["copies"] or 1,
                "page_count": of_row["page_count"] or 0,
                "duplex": of_row["duplex"] or "on",
                "page_range": of_row["page_range"] or "",
                "image_orientation": of_row["image_orientation"] or "auto",
                "total_price": round(of_row["total_price"] or 0.0, 2),
                "status": of_row["status"],
                "is_free": bool(of_row["is_free"]),
                "created_at": of_row["created_at"],
                "reject_reason": of_row["reject_reason"] or "",
            })

    # 全部子任务费用合计（含被排除状态；订单级附加服务费 = 父订单 total_price − 全部子任务费）
    all_file_sum = {}
    if order_ids:
        all_sum_rows = conn.execute(
            f"""SELECT order_id, SUM(total_price) AS file_sum
                FROM order_files
                WHERE order_id IN ({of_placeholders})
                GROUP BY order_id""",
            order_ids,
        ).fetchall()
        all_file_sum = {r["order_id"]: (r["file_sum"] or 0.0) for r in all_sum_rows}

    # 批量查昵称和头像
    all_openids = list(set(r["openid"] for r in order_rows if r["openid"] and r["openid"] != "local"))
    all_profiles = {}
    if all_openids:
        user_conn2 = get_user_db()
        ap_placeholders = ",".join("?" for _ in all_openids)
        nn_rows = user_conn2.execute(
            f"SELECT openid, nickname, avatar_path FROM users WHERE openid IN ({ap_placeholders})",
            all_openids,
        ).fetchall()
        for nr in nn_rows:
            all_profiles[nr["openid"]] = {
                "nickname": nr["nickname"] or "",
                "avatar_url": get_avatar_url(nr["openid"], nr["avatar_path"] or ""),
            }
        user_conn2.close()

    orders = []
    for row in order_rows:
        oid = row["id"]
        prof = all_profiles.get(row["openid"], {})
        files = files_by_order.get(oid, [])
        # 状态与移动端完全一致：用子任务实时聚合（orders.status 存储值可能滞后，
        # 如任务推送后回退 queued 未刷新父订单），聚合逻辑与 /api/orders 相同。
        agg_status = aggregate_order_status(conn, oid) or row["status"]
        # 金额口径：已入账 = sent 子任务合计；应收未收 = 其余未排除状态（排队/打印中/断线未知）合计；
        # 取消/放弃/打回/失败不计（失败单大概率不收费，仍可在明细状态列看到）。
        # 文件级 is_free 免费文件不计入金额但单独计数。
        # v5.1：自打订单（is_admin_print=1）不计入收益金额（明细文件单价仍原样展示）。
        paid_revenue = 0.0
        receivable = 0.0
        free_files = 0
        is_self_print = bool(row["is_admin_print"])
        for f in files:
            if f["is_free"]:
                free_files += 1
            if is_self_print:
                continue
            if f["status"] == "sent":
                paid_revenue += f["total_price"]
            elif f["status"] not in ("canceled", "abandoned", "rejected", "failed"):
                receivable += f["total_price"]
        # 订单级附加服务费（派送/加急/首页费）= 父订单 total_price − 全部子任务费。
        # 仅当订单存在「有效文件」（非取消/放弃/打回/失败）时计入，且按有效文件状态整体
        # 归入已入账（全部 sent）/ 应收未收（有未 sent）；自打订单 / 无有效文件（如整单放弃）不计。
        extra_fee = max(0.0, (row["total_price"] or 0.0) - all_file_sum.get(oid, 0.0))
        if extra_fee > 0 and not is_self_print and files:
            active_files = [f for f in files if f["status"] == "sent" or f["status"] not in ("canceled", "abandoned", "rejected", "failed")]
            if active_files:
                if all(f["status"] == "sent" for f in active_files):
                    paid_revenue += extra_fee
                else:
                    receivable += extra_fee
        orders.append({
            "order_id": oid,
            "order_number": row["order_number"] or "",
            "source": row["source"] or "wechat",
            "openid": row["openid"] or "",
            "nickname": prof.get("nickname", ""),
            "avatar_url": prof.get("avatar_url", ""),
            "status": agg_status,
            "total_price": round(row["total_price"] or 0.0, 2),
            "created_at": row["created_at"],
            "owner_name": row["owner_name"] or "",
            "is_admin_print": bool(row["is_admin_print"]),
            "delivery_enabled": bool(row["delivery_enabled"]),
            "delivery_location": row["delivery_location"] or "",
            "delivery_percentage": row["delivery_percentage"] or 0,
            "urgency": row["urgency"] or "低",
            "urgency_price": row["urgency_price"] or 0,
            "cover_page": bool(row["cover_page"]),
            "cover_page_price": row["cover_page_price"] or 0,
            "pickup_address": row["pickup_address"] or "",
            "remark": row["remark"] or "",
            "auto_print": bool(row["auto_print"]),
            "schedule_mode": row["schedule_mode"] or "now",
            "scheduled_at": row["scheduled_at"] or "",
            "print_started_at": row["print_started_at"] or "",
            "wait_seconds": calc_wait_seconds(row["created_at"], row["print_started_at"]),
            # 接单设备展示名（{所有者}的设备（{client_id}））；空值显示 —
            "received_client": row["received_client"] or "",
            "received_label": device_display_label(row["received_client"] or ""),
            "files_count": len(files),
            "total_page_count": sum(f["page_count"] * f["copies"] for f in files),
            "revenue": round(paid_revenue, 2),
            "receivable": round(receivable, 2),
            "free_files": free_files,
            "files": files,
        })

    # ── 基于订单明细统一聚合：按来源汇总 + 按用户分组 ──
    # 金额 = 已入账 + 应收未收（v5.17 合并口径，含订单级附加服务费）；自打订单金额已在上方剔除。
    for o in orders:
        src = o["source"] if o["source"] in ("wechat", "app", "local") else "wechat"
        rev = o["revenue"] + o["receivable"]
        revenue_summary["total"] += rev
        revenue_summary[src] += rev
        revenue_summary[f"{src}_orders"] += 1
        revenue_summary[f"{src}_files"] += o["files_count"]
        revenue_summary[f"{src}_pages"] += o["total_page_count"]
        revenue_summary["total_pages"] += o["total_page_count"]
    for _k in ("total", "wechat", "app", "local"):
        revenue_summary[_k] = round(revenue_summary[_k], 2)

    by_user_map = {}
    for o in orders:
        if not o["openid"] or o["openid"] == "local":
            continue
        key = o["openid"]
        u = by_user_map.get(key)
        if u is None:
            u = by_user_map[key] = {
                "openid": key,
                "nickname": o["nickname"],
                "avatar_url": o["avatar_url"],
                "revenue": 0.0,
                "order_count": 0,
                "wechat_orders": 0,
                "app_orders": 0,
                "file_count": 0,
                "total_pages": 0,
            }
        u["revenue"] += o["revenue"] + o["receivable"]
        u["order_count"] += 1
        if o["source"] == "wechat":
            u["wechat_orders"] += 1
        elif o["source"] == "app":
            u["app_orders"] += 1
        u["file_count"] += o["files_count"]
        u["total_pages"] += o["total_page_count"]
    by_user = sorted(by_user_map.values(), key=lambda u: u["revenue"], reverse=True)
    for u in by_user:
        u["revenue"] = round(u["revenue"], 2)

    conn.close()

    return jsonify({
        "success": True,
        "data": {
            "period": {"start": start_date, "end": end_date},
            "revenue_summary": revenue_summary,
            "by_user": by_user,
            "orders": orders,
        },
    })


@app.route("/api/admin/statistics/owners", methods=["GET"])
def admin_statistics_owners():
    """管理员订单归属统计（printer token 鉴权）。
    按订单归属管理员（orders.owner_name）分组，拆分"管理员自己的订单"（is_admin_print=1）
    与"接单打印的顾客订单"（is_admin_print=0），供本地打印工具收支清算页展示每位管理员的
    自打/接单比例与整体明细。
    参数: start_date / end_date / status（同 revenue 接口）
    """
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    if not start_date or not end_date:
        return jsonify({"success": False, "message": "缺少 start_date 或 end_date 参数"}), 400

    # 状态筛选（排除已取消/已拒绝/已放弃/已预留），口径与 revenue 接口一致
    status_param = request.args.get("status", "").strip()
    if status_param:
        allowed_statuses = [s.strip() for s in status_param.split(",") if s.strip()]
    else:
        allowed_statuses = ["sent", "printing", "queued", "failed", "offline_unknown"]

    conn = get_db()
    status_placeholders = ",".join("?" for _ in allowed_statuses)
    params = [start_date + " 00:00:00", end_date + " 23:59:59"] + allowed_statuses

    # ── 按归属管理员分组（自打 / 接单两维度） ──
    rows = conn.execute(
        f"""SELECT o.owner_name,
                   COUNT(DISTINCT o.id) AS order_count,
                   COUNT(DISTINCT CASE WHEN o.is_admin_print = 1 THEN o.id END) AS own_orders,
                   COUNT(DISTINCT CASE WHEN o.is_admin_print = 0 THEN o.id END) AS accepted_orders,
                   COUNT(DISTINCT CASE WHEN o.is_admin_print = 1 THEN of.id END) AS own_files,
                   COUNT(DISTINCT CASE WHEN o.is_admin_print = 0 THEN of.id END) AS accepted_files,
                   SUM(CASE WHEN o.is_admin_print = 1 THEN of.page_count * of.copies ELSE 0 END) AS own_pages,
                   SUM(CASE WHEN o.is_admin_print = 0 THEN of.page_count * of.copies ELSE 0 END) AS accepted_pages,
                   SUM(CASE WHEN o.is_admin_print = 1 AND of.status = 'sent' THEN of.total_price ELSE 0 END) AS own_revenue,
                   SUM(CASE WHEN o.is_admin_print = 0 AND of.status = 'sent' THEN of.total_price ELSE 0 END) AS accepted_revenue
            FROM orders o
            INNER JOIN order_files of ON o.id = of.order_id
            WHERE o.created_at >= ? AND o.created_at <= ?
              AND of.status IN ({status_placeholders})
            GROUP BY o.owner_name
            ORDER BY order_count DESC""",
        params,
    ).fetchall()
    conn.close()

    owners = []
    totals = {"order_count": 0,
              "own_orders": 0, "accepted_orders": 0,
              "own_files": 0, "accepted_files": 0,
              "own_pages": 0, "accepted_pages": 0,
              "own_revenue": 0.0, "accepted_revenue": 0.0}
    for row in rows:
        name = (row["owner_name"] or "").strip() or "未归属"
        own = row["own_orders"] or 0
        acc = row["accepted_orders"] or 0
        own_rev = round(row["own_revenue"] or 0.0, 2)
        acc_rev = round(row["accepted_revenue"] or 0.0, 2)
        owners.append({
            "owner_name": name,
            "order_count": row["order_count"] or 0,
            "own_orders": own,
            "accepted_orders": acc,
            "own_ratio": round(own / (own + acc), 4) if (own + acc) else 0,
            "own_files": row["own_files"] or 0,
            "accepted_files": row["accepted_files"] or 0,
            "own_pages": row["own_pages"] or 0,
            "accepted_pages": row["accepted_pages"] or 0,
            "own_revenue": own_rev,
            "accepted_revenue": acc_rev,
        })
        totals["order_count"] += row["order_count"] or 0
        for k in ("own_orders", "accepted_orders", "own_files", "accepted_files",
                  "own_pages", "accepted_pages"):
            totals[k] += row[k] or 0
        totals["own_revenue"] = round(totals["own_revenue"] + own_rev, 2)
        totals["accepted_revenue"] = round(totals["accepted_revenue"] + acc_rev, 2)

    return jsonify({
        "success": True,
        "data": {
            "period": {"start": start_date, "end": end_date},
            "owners": owners,
            "summary": totals,
        },
    })


@app.route("/api/admin/statistics/months", methods=["GET"])
def admin_statistics_months():
    """按月份统计订单/文件数与收益（printer token 鉴权），供收支清算云端模块自动列出月份。
    收益口径沿用 revenue 接口：of.status='sent' 的 total_price 合计。
    """
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT strftime('%Y-%m', o.created_at) AS month,
                      COUNT(DISTINCT o.id) AS order_count,
                      COUNT(of.id) AS file_count,
                      SUM(CASE WHEN of.status = 'sent' THEN of.total_price ELSE 0 END) AS revenue
               FROM orders o
               INNER JOIN order_files of ON o.id = of.order_id
               WHERE o.created_at IS NOT NULL
               GROUP BY month
               ORDER BY month"""
        ).fetchall()
    finally:
        conn.close()

    months = []
    for row in rows:
        month = row["month"]
        try:
            y, m = month.split("-")
            label = f"{int(y)}年{int(m)}月"
        except Exception:
            label = month
        months.append({
            "month": month,
            "label": label,
            "orders": row["order_count"] or 0,
            "files": row["file_count"] or 0,
            "revenue": round(row["revenue"] or 0.0, 2),
        })
    return jsonify({"success": True, "data": {"months": months}})


def recover_stale_printing_tasks():
    """启动时清理孤立的 printing 子任务。

    pushed_tasks（记录已推送待回报的子任务）是内存结构，进程重启后会丢失。
    若上次进程在 emit 之后、回报之前崩溃，order_files 会永久停留在
    'printing' 而无人处理（表现：队列里有任务但打印机不动，或每次拉取都重复
    处理）。这里把所有 printing 子任务重置为 queued，让定时扫描/拉取接口
    重新分发。客户端的幂等处理（pull 后立即标记 printing）可防止重复打印。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, order_id FROM order_files WHERE status = 'printing'"
    ).fetchall()
    count = 0
    for row in rows:
        conn.execute(
            "UPDATE order_files SET status = 'queued' WHERE id = ?", (row["id"],)
        )
        refresh_order_status(conn, row["order_id"])
        count += 1
    conn.commit()
    conn.close()
    if count > 0:
        print(f"  [RECOVER] 重置 {count} 个孤立 printing 子任务为 queued")
    return count


# ==================== 定时任务调度器（模块级，供 Gunicorn worker 钩子引用）========

scheduler = BackgroundScheduler()


def cleanup_abandoned_reserved_orders():
    """清理超过 30 分钟仍未提交的 reserved 占位订单。
    场景：用户在本地工具中点击了"复制"（分配了订单号）但从未点击"开始打印"，
    或者获取订单号后离线了且从未同步成功。
    纯占位单（未配置任何文件/子任务，file 仍为‘（预留位置）’、total_price=0）没有归属与金额，
    直接删除整行，避免留下 owner_name 为空、统计口径外的“无归属 abandoned”残留订单。"""
    cutoff = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        # 先删除纯占位单（从未配置文件/子任务，无金额）→ 不留无归属残留
        cursor = conn.execute(
            """DELETE FROM orders WHERE status = 'reserved'
               AND created_at < ?
               AND file = '（预留位置）'
               AND total_price <= 0
               AND NOT EXISTS (SELECT 1 FROM order_files f WHERE f.order_id = orders.id)""",
            (cutoff,),
        )
        count = cursor.rowcount
        if count > 0:
            conn.commit()
            print(f"[CLEANUP] 已删除 {count} 个超时纯占位订单（无归属残留）")
        # 剩余带配置的 reserved（含复制价格后完整预留的订单）标记 abandoned，子任务同步置 abandoned
        cursor2 = conn.execute(
            "UPDATE orders SET status = 'abandoned' WHERE status = 'reserved' AND created_at < ?",
            (cutoff,),
        )
        count2 = cursor2.rowcount
        if count2 > 0:
            conn.execute(
                "UPDATE order_files SET status = 'abandoned'"
                " WHERE order_id IN (SELECT id FROM orders WHERE status = 'abandoned'"
                "   AND created_at < ?) AND status NOT IN ('sent', 'failed', 'canceled')",
                (cutoff,),
            )
            conn.commit()
            print(f"[CLEANUP] 已将 {count2} 个超时 reserved 订单标记为 abandoned（含子任务）")
    except Exception as e:
        conn.rollback()
        print(f"[CLEANUP] 清理 reserved 订单失败: {e}")
    finally:
        conn.close()


# ==================== 日志系统 ====================

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_server_log = logging.getLogger("printer_server")
_server_log.setLevel(logging.WARNING)
if not _server_log.handlers:
    _sh = logging.FileHandler(os.path.join(LOG_DIR, "server.log"), encoding="utf-8")
    _sh.setLevel(logging.WARNING)
    _sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _server_log.addHandler(_sh)

_frontend_log = logging.getLogger("printer_frontend")
_frontend_log.setLevel(logging.WARNING)
if not _frontend_log.handlers:
    _fh = logging.FileHandler(os.path.join(LOG_DIR, "frontend.log"), encoding="utf-8")
    _fh.setLevel(logging.WARNING)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _frontend_log.addHandler(_fh)


_FRONTEND_LOG_PATH = os.path.join(LOG_DIR, "frontend.log")
_FRONTEND_LOG_MAX_BYTES = 50 * 1024 * 1024  # 超过 50MB 轮转（P1-6）


def _write_frontend_log(level, message):
    """写前端日志（手动追加 + 超 50MB 轮转）。
    logging.FileHandler 在 Windows 上持有文件句柄，写期间无法重命名，故手动管理。"""
    if os.path.exists(_FRONTEND_LOG_PATH) and os.path.getsize(_FRONTEND_LOG_PATH) > _FRONTEND_LOG_MAX_BYTES:
        try:
            if os.path.exists(_FRONTEND_LOG_PATH + ".1"):
                os.remove(_FRONTEND_LOG_PATH + ".1")
            os.rename(_FRONTEND_LOG_PATH, _FRONTEND_LOG_PATH + ".1")
            print(f"[LOG] frontend.log 超过 {_FRONTEND_LOG_MAX_BYTES // (1024 * 1024)}MB，已轮转为 .1")
        except OSError as e:
            print(f"[LOG] 前端日志轮转失败: {e}")
    try:
        with open(_FRONTEND_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level.upper()}] {message}\n")
    except OSError as e:
        print(f"[LOG] 写前端日志失败: {e}")


@app.route("/api/log/report", methods=["POST"])
def log_report():
    """前端上报错误/警告日志。
    P1-6.2：新增鉴权（打印机 token 或登录态 Bearer）、每 IP 每分钟 30 条限速、
    message 长度限制 4KB（超长截断）、frontend.log 超 50MB 自动轮转。"""
    # 鉴权：打印机 token（X-Printer-Token / ?token=）或登录态（Bearer token）二选一
    authorized = False
    if PRINTER_TOKEN and _get_printer_token() == PRINTER_TOKEN:
        authorized = True
    else:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            try:
                token_serializer.loads(auth[7:], max_age=TOKEN_MAX_AGE)
                authorized = True
            except Exception:
                authorized = False
    if not authorized:
        return jsonify({"success": False, "message": "未授权"}), 403

    # 限速（P1-6 / 防滥用）：每 IP 每分钟日志上报条数上限（管理员可调，单 worker 内存计数）
    ip = request.remote_addr or "unknown"
    if not _rate_limit(f"log_report:{ip}", _SECURITY["log_report_rate_limit"], 60):
        return jsonify({"success": False, "message": "请求过于频繁"}), 429

    data = request.get_json(silent=True) or {}
    level = data.get("level", "warning")
    message = data.get("message", "")
    if not message:
        return jsonify({"success": False, "message": "缺少日志内容"}), 400
    if len(message) > 4096:
        message = message[:4096]  # 截断而非拒绝，防止日志文件被撑爆
    if level in ("error", "critical"):
        _write_frontend_log("error", message)
    else:
        _write_frontend_log("warning", message)
    return jsonify({"success": True})


@app.route("/api/log/fetch", methods=["GET"])
def log_fetch():
    """本地打印工具拉取后端/前端日志（需 token 认证）。
    P2-14：支持 ?tail=N 只返回末尾 N 行（默认 500，0 = 不返回内容，仅取大小）。"""
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    log_type = request.args.get("type", "server")
    if log_type not in ("server", "frontend"):
        return jsonify({"success": False, "message": "type 只能为 server 或 frontend"}), 400
    try:
        tail = int(request.args.get("tail", "500"))
    except (ValueError, TypeError):
        tail = 500
    tail = max(0, tail)
    log_path = os.path.join(LOG_DIR, f"{log_type}.log")
    if not os.path.exists(log_path):
        return jsonify({"success": True, "size": 0, "content": ""})
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if tail == 0:
        content = ""
    else:
        content = "".join(lines[-tail:])
    return jsonify({"success": True, "size": len(content.encode("utf-8")), "content": content})


@app.route("/api/log/collect_all", methods=["POST"])
def log_collect_all():
    """收集所有在线打印设备的日志（本地打印工具「日志管理」页发起，需打印机 token）。

    向所有在线客户端 SocketIO 推送 request_log（附本次 request_id），等待各设备
    回报本机日志尾部（logs/local_tool.log 末尾 200KB）；超时未回报的设备标记 error。
    body: {"timeout": 秒（2-20，默认 8）}
    response: {"success": true, "logs": [{client_id, device_name, online, content | error}]}
    """
    global _collect_request_id
    token = _get_printer_token()
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    try:
        timeout = max(2, min(20, int(data.get("timeout", 8) or 8)))
    except (ValueError, TypeError):
        timeout = 8

    active = get_active_clients()
    if not active:
        return jsonify({"success": True, "logs": []})

    with _collect_logs_lock:
        _collect_request_id += 1
        request_id = _collect_request_id
        _collect_logs.clear()

    # 向所有在线打印机客户端下发收集指令
    for cid in active:
        with printer_clients_lock:
            info = printer_clients.get(cid)
            sid = info["sid"] if info else None
        if sid:
            try:
                socketio.emit("request_log", {"request_id": request_id, "max_bytes": _COLLECT_LOG_MAX_BYTES}, to=sid)
            except Exception as e:
                print(f"  [LOG] 下发 request_log 到 {cid} 失败: {e}")

    # 等待各设备回报（轮询；eventlet 下 sleep 让出协程，socketio 事件可并发处理）
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _collect_logs_lock:
            received = set(_collect_logs.keys())
        if received >= set(active):
            break
        time.sleep(0.2)

    with _collect_logs_lock:
        collected = dict(_collect_logs)

    devices = load_devices()
    logs = []
    for cid in active:
        entry = devices.get(cid) or {}
        item = {
            "client_id": cid,
            "device_name": entry.get("device_name", "") or cid,
            "online": True,
        }
        content = collected.get(cid)
        if content is None:
            item["error"] = "超时未回报"
        else:
            item["content"] = content
        logs.append(item)
    collected_count = sum(1 for l in logs if "content" in l)
    print(f"[LOG] 在线设备日志收集完成: {collected_count}/{len(logs)} 台回报（timeout={timeout}s）")
    return jsonify({"success": True, "logs": logs, "collected": collected_count})


# ==================== 启动 ====================

if __name__ == "__main__":
    init_db()
    print("数据库已初始化")

    scheduler.add_job(process_pending_orders, "interval", seconds=30, id="scan_orders")
    scheduler.add_job(process_scheduled_orders, "interval", seconds=30, id="scan_scheduled")
    scheduler.add_job(check_printing_timeout, "interval", seconds=60, id="check_timeout")
    scheduler.add_job(cleanup_expired_license_keys, "interval", minutes=10, id="cleanup_licenses")
    scheduler.add_job(cleanup_expired_files, "interval", minutes=10, id="cleanup_files")
    scheduler.add_job(recover_orphaned_printing_tasks, "interval", minutes=2, id="recover_orphans")
    scheduler.add_job(recover_stale_downloading, "interval", minutes=2, id="recover_stale_downloads")
    scheduler.add_job(cleanup_abandoned_reserved_orders, "interval", minutes=5, id="cleanup_reserved")
    scheduler.add_job(expire_stale_queued_orders, "interval", minutes=10, id="expire_stale_queued")
    scheduler.start()
    print("定时扫描已启动（任务扫描每 30s，预约扫描每 30s，超时检查每 60s，预留订单清理每 5min）")

    # P2-8：debug 直跑仅限开发（FLASK_DEBUG=1）；生产必须用 gunicorn（systemd 服务），
    # 直跑模式是单进程开发服务器，不用于生产。
    socketio.run(app, host="127.0.0.1", port=5000,
                 debug=os.environ.get("FLASK_DEBUG") == "1", use_reloader=False,
                 allow_unsafe_werkzeug=True)
