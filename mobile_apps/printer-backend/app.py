import hashlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import string
import secrets
import threading
import time
import uuid
import math
from datetime import datetime, timedelta
from functools import wraps
from urllib import request as urlrequest, parse as urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, g, request, jsonify, send_file
from flask_socketio import SocketIO, emit, disconnect, join_room
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 最大上传 50MB
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DATABASE = "orders.db"
UPLOAD_DIR = "uploads"
AVATAR_DIR = os.path.join(UPLOAD_DIR, "avatars")
MD5_INDEX_FILE = os.path.join(UPLOAD_DIR, "md5_index.json")
RETENTION_CONFIG_FILE = os.path.join(UPLOAD_DIR, "retention_config.json")

# 默认保留时间：7 天
DEFAULT_RETENTION = {"days": 7, "hours": 0}

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
    if not os.path.exists(MD5_INDEX_FILE):
        return {}
    try:
        with open(MD5_INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_md5_index(index):
    """保存 MD5 索引到文件"""
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
    """清理超过保留时间的文件（删磁盘文件，不删数据库记录）"""
    cfg = load_retention_config()
    days = cfg.get("days", 0)
    hours = cfg.get("hours", 0)

    # 0 天 0 小时 = 永不过期
    if days == 0 and hours == 0:
        return

    cutoff = datetime.now() - timedelta(days=days, hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    rows = conn.execute(
        "SELECT id, path FROM files WHERE created_at < ? AND path != ''",
        (cutoff_str,),
    ).fetchall()
    conn.close()

    if not rows:
        return

    md5_index = load_md5_index()
    deleted_count = 0

    for row in rows:
        file_id = row["id"]
        file_path = row["path"]

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

        # 清空 files 表中的路径（保留记录本身）
        conn = get_db()
        conn.execute("UPDATE files SET path = '', size = 0 WHERE id = ?", (file_id,))
        conn.commit()
        conn.close()

        deleted_count += 1

    if deleted_count > 0:
        save_md5_index(md5_index)
        print(f"  [CLEANUP] 已清理 {deleted_count} 个过期文件（cutoff={cutoff_str}）")


# -------- 加载配置 --------
try:
    import config

    # 微信配置（用 getattr 兼容旧 config.py 缺少这些字段的情况）
    WECHAT_APPID = getattr(config, "WECHAT_APPID", None)
    WECHAT_APPSECRET = getattr(config, "WECHAT_APPSECRET", None)
    SECRET_KEY = getattr(config, "SECRET_KEY", None)
    PUBLIC_BASE_URL = getattr(config, "PUBLIC_BASE_URL", "http://127.0.0.1:5000")
    if WECHAT_APPID and WECHAT_APPSECRET and SECRET_KEY:
        print("已加载 config.py 微信配置")
    else:
        print("[WARN] config.py 中缺少微信配置项（WECHAT_APPID / WECHAT_APPSECRET / SECRET_KEY），登录功能不可用")
except ImportError:
    print("[WARN] 未找到 config.py，请复制 config.py.example → config.py")
    WECHAT_APPID = None
    WECHAT_APPSECRET = None
    SECRET_KEY = None

# 管理员列表（从 config.py 加载，用于统计与权限控制）
try:
    ADMIN_OPENIDS = set(getattr(config, "ADMIN_OPENIDS", []))
    if ADMIN_OPENIDS:
        print(f"已加载 {len(ADMIN_OPENIDS)} 个管理员 openid")
except Exception:
    ADMIN_OPENIDS = set()

# 超级管理员（从 config.py 加载，可创建 admin 类型许可密钥）
try:
    SUPER_ADMIN_OPENID = getattr(config, "SUPER_ADMIN_OPENID", None)
except Exception:
    SUPER_ADMIN_OPENID = None


def is_admin(openid):
    """判断给定 openid 是否为管理员"""
    return openid in ADMIN_OPENIDS


def compute_role(openid):
    """计算用户角色: admin / user / guest（含 DB 中 admin 角色和临时授权）"""
    if is_admin(openid):
        return "admin"
    conn = get_db()
    row = conn.execute("SELECT role, temp_until FROM users WHERE openid = ?", (openid,)).fetchone()
    conn.close()
    if row:
        if row["role"] == "admin":
            return "admin"
        if row["role"] == "user":
            return "user"
        # 临时授权：temp_until 未过期视为 user
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


# Token 签名器（依赖 SECRET_KEY，必须在配置加载之后初始化）
app.config["SECRET_KEY"] = SECRET_KEY or "fallback-dev-key-please-change-in-production"
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

# 打印机客户端认证 token
try:
    PRINTER_TOKEN = config.TOKEN
except (NameError, AttributeError):
    PRINTER_TOKEN = None


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


def make_download_url(file_id):
    """生成带签名的文件下载 URL（1 小时有效）"""
    token = download_serializer.dumps(file_id)
    return f"{PUBLIC_BASE_URL}/api/download/{file_id}?t={token}"


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


def _count_pages_via_libreoffice(file_path: str, ext: str) -> int:
    """通过 LibreOffice 将文档转为临时 PDF，再用 pypdf 统计页数。
    成功返回页数（≥1），失败返回 0 表示未知。"""
    import subprocess
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmpdir, file_path],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                stderr_tail = (result.stderr or "").strip()[-200:]
                print(f"  [WARN] LibreOffice 转换失败 (rc={result.returncode}): {stderr_tail}")
                return 0
            # LibreOffice 输出的 PDF 文件名 = 原始 basename 改扩展名 .pdf
            base = os.path.splitext(os.path.basename(file_path))[0]
            for fname in os.listdir(tmpdir):
                if fname.lower().endswith(".pdf"):
                    pdf_path = os.path.join(tmpdir, fname)
                    from pypdf import PdfReader
                    reader = PdfReader(pdf_path)
                    page_count = len(reader.pages)
                    print(f"  [PAGE] {ext.upper()} {os.path.basename(file_path)}: {page_count} 页 (via LibreOffice)")
                    return max(page_count, 1)
            print(f"  [WARN] LibreOffice 未生成预期 PDF（base={base}），目录内容: {os.listdir(tmpdir)}")
            return 0
    except FileNotFoundError:
        print(f"  [WARN] LibreOffice (soffice) 未安装，.{ext} 文件页数无法获取")
    except subprocess.TimeoutExpired:
        print(f"  [WARN] LibreOffice 转换超时 (15s)，.{ext} 文件页数未知")
    except Exception as e:
        print(f"  [WARN] LibreOffice 转换异常 ({e})，.{ext} 文件页数未知")
    return 0


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
    # 开启 WAL 模式：允许多个读连接与一个写连接并发，是解决
    # "database is locked" 的关键。PRAGMA 在每个连接上设置，但 WAL
    # 标志一旦设置就会持久化到数据库文件，这里重复设置确保旧库也生效。
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError as e:
        print(f"  [WARN] 开启 WAL 模式失败: {e}")
    conn.commit()
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

    # 用户表（头像、昵称）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            openid      TEXT PRIMARY KEY,
            nickname    TEXT DEFAULT '',
            avatar_path TEXT DEFAULT '',
            updated_at  TEXT NOT NULL
        )
        """
    )

    # v3 迁移：删除预约字段（SQLite 3.35+ 支持 DROP COLUMN）
    for col in ["reservation_date", "reservation_time"]:
        try:
            conn.execute(f"ALTER TABLE orders DROP COLUMN {col}")
            conn.commit()
            print(f"  已删除旧字段: {col}")
        except sqlite3.OperationalError:
            pass  # 字段不存在或 SQLite 版本不支持，忽略

    # v4 迁移：给 users 表添加 role 列
    try:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'guest'")
        conn.commit()
        print("  已添加 users.role 列")
    except sqlite3.OperationalError:
        pass

    # v5 迁移：订单附加服务字段（派送/紧急/首页/地址）
    for col, col_type, default in [
        ("delivery_enabled", "INTEGER", "0"),
        ("delivery_location", "TEXT", "''"),
        ("delivery_percentage", "REAL", "0"),
        ("urgency", "TEXT", "'低'"),
        ("urgency_price", "REAL", "0"),
        ("cover_page", "INTEGER", "0"),
        ("cover_page_price", "REAL", "0.15"),
        ("pickup_address", "TEXT", "''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
            print(f"  已添加 orders.{col} 列")
        except sqlite3.OperationalError:
            pass

    # 许可密钥表
    conn.execute(
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

    # v9 迁移：license_keys 添加 type 列（temp 表示临时许可，admin 表示永久管理许可）
    try:
        conn.execute("ALTER TABLE license_keys ADD COLUMN type TEXT DEFAULT 'temp'")
        conn.commit()
        print("  已添加 license_keys.type 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v10 迁移：users 添加 temp_until 列（临时用户有效期截止时间，永久用户为 NULL）
    try:
        conn.execute("ALTER TABLE users ADD COLUMN temp_until TEXT DEFAULT NULL")
        conn.commit()
        print("  已添加 users.temp_until 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v11 迁移：license_keys 添加 order_id 列（临时用户提交订单后关联）
    try:
        conn.execute("ALTER TABLE license_keys ADD COLUMN order_id INTEGER DEFAULT NULL")
        conn.commit()
        print("  已添加 license_keys.order_id 列")
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

    conn.commit()
    conn.close()


# ==================== 订单号生成 ====================

@app.route("/api/next_order_number", methods=["GET"])
def next_order_number():
    """本地打印工具获取下一个可用订单号（需 token 认证）。
    调用即分配，保证本地和云端订单号不冲突。"""
    token = request.args.get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    order_number = generate_order_number()
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

    return f"HN{today}-{counter:04d}"


# ==================== 价格计算 ====================

def calculate_price(page_count, duplex):
    """根据页数和双面模式计算价格（单份文件，不含份数倍率）。
    单面打印: 0.2 元/页
    双面打印: 0.3 元/张（每张纸可印两页，奇数页最后一张按单面 0.2 元计费）
    """
    if duplex == "on":
        sheets = math.ceil(page_count / 2)
        odd_pages = page_count % 2
        price = (sheets - odd_pages) * 0.3 + odd_pages * 0.2
    else:
        price = page_count * 0.2
    return round(price, 2)


# ==================== 订单状态聚合 ====================

# 状态优先级：失败 > 打印中/排队中 > 已完成 > 被打回 > 已取消
# 用于把多个子任务 (order_files) 的状态聚合为父订单 (orders) 的状态
_STATUS_PRIORITY = {"failed": 8, "printing": 7, "accepted": 6, "offline_unknown": 5, "queued": 4, "sent": 3, "abandoned": 2, "rejected": 1, "canceled": 0}


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
        # 混合终态：有 sent 优先，有 offline_unknown 次之
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


# ==================== 原子任务领取 ====================


def fetch_and_lock_task(client_id):
    """原子化地获取一个 queued 任务并立即锁定为 printing，返回完整任务字典或 None。
    使用全局 db_lock 确保多台打印机并发拉取时不会重复分配同一任务。"""
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM order_files WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            task_id = row["id"]
            # 锁定任务并记录领取者
            conn.execute(
                "UPDATE order_files SET status = 'printing', operator_client = ? WHERE id = ?",
                (client_id, task_id)
            )
            # 刷新父订单聚合状态
            parent_row = conn.execute(
                "SELECT order_id FROM order_files WHERE id = ?", (task_id,)
            ).fetchone()
            if parent_row:
                refresh_order_status(conn, parent_row["order_id"])
            conn.commit()
            # 重新查询完整数据返回（含父订单信息 + 文件 MD5）
            full_task = conn.execute(
                """SELECT of.*, o.order_number, o.delivery_enabled, o.delivery_location,
                          o.urgency, o.cover_page, o.cover_page_price,
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
    """扫描排队中的子任务（order_files）：当打印机客户端上线时，推送排队任务"""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT of.id AS of_id, of.order_id, of.file_id, of.file_name,
               of.copies, of.page_range, of.duplex
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
    active_clients = get_active_clients()
    print(f"\n[{now_str}] 扫描到 {len(rows)} 个排队子任务, {len(active_clients)} 个活跃客户端")

    if not active_clients:
        print(f"  无活跃打印机客户端，等待下次扫描")
        return

    for row in rows:
        of_id = row["of_id"]
        order_id = row["order_id"]
        file_id = row["file_id"]
        file_name = row["file_name"]
        copies = row["copies"]
        page_range = row["page_range"] or ""
        duplex = row["duplex"]

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
                "UPDATE order_files SET status = 'failed' WHERE id = ?",
                (of_id,),
            )
            refresh_order_status(conn, order_id)
            _retry_on_lock(conn.commit)
            conn.close()
            continue

        # 推送给打印机客户端
        active_clients = get_active_clients()
        if active_clients:
            pushed = push_print_task_to_client(of_id, file_id, file_name, copies, duplex, page_range, active_clients[0])
            if pushed:
                continue

        # 推送失败：保持 queued，等待下次扫描
        print(f"  [WAIT] 子任务 #{of_id}: 推送失败，保持排队")


def push_print_task_to_client(sub_task_id, file_id, file_name, copies, duplex, page_range, client_id):
    """通过 SocketIO 推送子任务 (order_files) 到指定打印机客户端。
    sub_task_id = order_files.id，推送后更新该子任务状态为 printing。

    关键顺序：先在数据库中把子任务标记为 printing（并登记 pushed_tasks），
    再通过 SocketIO emit。这样即使 emit 之后客户端来不及回报，数据库状态
    也是一致的；若数据库写失败（锁等），任务保持 queued 不会被推送，避免
    出现"客户端已处理但数据库仍 queued"的幽灵任务。"""
    if not file_id:
        return False

    # 1. 原子锁定任务（db_lock 内使用独立连接，防止与 pull 接口并发重复推送）
    order_id = None
    with db_lock:
        conn = get_db_conn()
        try:
            cur = conn.execute(
                "UPDATE order_files SET status = 'printing', operator_client = ? WHERE id = ? AND status = 'queued'",
                (client_id, sub_task_id),
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
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # 2. 登记推送等待反馈（必须在 emit 之前登记，避免回调先到导致漏清理）
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
        "options": {
            "copies": copies,
            "duplex": duplex or "on",
            "page_range": page_range or "",
        },
    }

    # 查询父订单的附加服务配置，传递给本地工具
    if order_id:
        conn2 = get_db()
        o_row = conn2.execute(
            "SELECT delivery_enabled, delivery_location, urgency, cover_page, cover_page_price FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        conn2.close()
        if o_row:
            task_msg["delivery_enabled"] = bool(o_row["delivery_enabled"])
            task_msg["delivery_location"] = o_row["delivery_location"] or ""
            task_msg["urgency"] = o_row["urgency"] or "低"
            task_msg["cover_page"] = bool(o_row["cover_page"])
            task_msg["cover_page_price"] = float(o_row["cover_page_price"] or 0.15)

    # 查询文件 MD5（供本地工具 PDF 缓存命中，避免重复下载）
    if file_id:
        conn = get_db()
        row = conn.execute("SELECT md5 FROM files WHERE id = ?", (file_id,)).fetchone()
        conn.close()
        if row and row["md5"]:
            task_msg["source_md5"] = row["md5"]

    if not sid:
        # 客户端刚断开：回滚为 queued，等下次扫描重试
        with db_lock:
            conn = get_db_conn()
            try:
                conn.execute(
                    "UPDATE order_files SET status = 'queued' WHERE id = ? AND status = 'printing'",
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
        # emit 失败：回滚为 queued，清理登记
        with db_lock:
            conn = get_db_conn()
            try:
                conn.execute(
                    "UPDATE order_files SET status = 'queued' WHERE id = ? AND status = 'printing'",
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
        with pushed_tasks_lock:
            pushed_tasks.pop(sub_task_id, None)
        return False


def check_printing_timeout():
    """检查超过超时时间未反馈的 printing 子任务 (order_files)，标记为失败并聚合父订单"""
    now = datetime.now()
    timeout_sub_tasks = []

    with pushed_tasks_lock:
        for sub_task_id, info in list(pushed_tasks.items()):
            if (now - info["pushed_at"]).total_seconds() > PRINT_FEEDBACK_TIMEOUT:
                timeout_sub_tasks.append(sub_task_id)
                del pushed_tasks[sub_task_id]

    if not timeout_sub_tasks:
        return

    print(f"\n[TIMEOUT] 超时检查: {len(timeout_sub_tasks)} 个子任务超时")
    for sub_task_id in timeout_sub_tasks:
        conn = get_db()
        of_row = conn.execute(
            "SELECT id, status, order_id FROM order_files WHERE id = ?", (sub_task_id,)
        ).fetchone()

        if of_row and of_row["status"] == "printing":
            print(f"  [FAIL] 子任务 #{sub_task_id}: 超时未反馈，标记为失败")
            conn.execute("UPDATE order_files SET status = 'failed' WHERE id = ?", (sub_task_id,))
            refresh_order_status(conn, of_row["order_id"])
            conn.commit()
        elif of_row and of_row["status"] != "printing":
            print(f"  [INFO] 子任务 #{sub_task_id}: 状态已变更为 {of_row['status']}，跳过")
        conn.close()


def recover_orphaned_printing_tasks():
    """扫描超过 5 分钟仍处于 printing 的任务，检查文件是否存在：
    - 文件存在 → 回退为 queued（让其他打印机重试）
    - 文件不存在 → 标记为 failed（避免无限循环）
    覆盖极端场景：服务器断电/客户端失联但未触发 disconnect 事件。"""
    cutoff_time = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_db_conn()
        rows = conn.execute(
            """SELECT of.id, of.order_id, of.file_id, f.path
               FROM order_files of
               LEFT JOIN files f ON of.file_id = f.id
               WHERE of.status = 'printing' AND of.created_at < ?""",
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
                f"UPDATE order_files SET status = 'queued', operator_client = '' WHERE id IN ({placeholders})",
                [str(x) for x in reset_ids]
            )
        if fail_ids:
            placeholders = ",".join("?" for _ in fail_ids)
            conn.execute(
                f"UPDATE order_files SET status = 'failed' WHERE id IN ({placeholders})",
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


# ==================== SocketIO 事件 ====================


@socketio.on("connect")
def on_connect(auth=None):
    """打印机客户端连接 -- 验证 URL 查询参数中的 token"""
    token = request.args.get("token", "")
    if not token and auth and isinstance(auth, dict):
        token = auth.get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        print(f"[WARN] 打印机客户端认证失败: token 无效")
        emit("auth_fail", {"message": "token 无效"})
        disconnect()
        return False

    client_id = request.args.get("client_id", request.sid)
    with printer_clients_lock:
        printer_clients[client_id] = {
            "sid": request.sid,
            "heartbeat": datetime.now(),
            "connected_at": datetime.now(),
        }
    join_room(client_id)
    print(f"[LINK] 打印机客户端已连接: {client_id}")


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
                    conn.execute(
                        f"UPDATE order_files SET status = 'queued', operator_client = '' WHERE id IN ({placeholders})",
                        ids
                    )
                    for r in rows:
                        refresh_order_status(conn, r["order_id"])
                    print(f"[RECOVER] 客户端 {client_id} 断开，已回滚 {len(rows)} 个任务")

                # 已接受但未打印的任务：标记为"断线未知"
                accepted_rows = conn.execute(
                    "SELECT id, order_id FROM order_files WHERE status = 'accepted' AND operator_client = ?",
                    (client_id,)
                ).fetchall()
                if accepted_rows:
                    a_ids = [str(r["id"]) for r in accepted_rows]
                    a_placeholders = ",".join("?" for _ in accepted_rows)
                    conn.execute(
                        f"UPDATE order_files SET status = 'offline_unknown' WHERE id IN ({a_placeholders})",
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


@socketio.on("print_success")
def on_print_success(data):
    """打印成功 -- 更新子任务状态为 sent，并聚合父订单状态"""
    task_id = data.get("task_id")
    if not task_id:
        return

    task_id = int(task_id)

    conn = get_db()
    conn.execute(
        "UPDATE order_files SET status = 'sent' WHERE id = ? AND status IN ('printing', 'accepted', 'offline_unknown')",
        (task_id,),
    )
    # 获取父订单 ID 并刷新聚合状态
    row = conn.execute("SELECT order_id FROM order_files WHERE id = ?", (task_id,)).fetchone()
    if row:
        refresh_order_status(conn, row["order_id"])
    conn.commit()
    conn.close()

    with pushed_tasks_lock:
        pushed_tasks.pop(task_id, None)

    print(f"  [OK] 子任务 #{task_id}: 客户端确认打印成功")


@socketio.on("print_fail")
def on_print_fail(data):
    """打印失败 -- 更新子任务状态为 failed，并聚合父订单状态"""
    task_id = data.get("task_id")
    error = data.get("error", "未知错误")

    if not task_id:
        return

    task_id = int(task_id)

    print(f"  [FAIL] 子任务 #{task_id}: 客户端打印失败 ({error})，标记为失败")
    conn = get_db()
    conn.execute("UPDATE order_files SET status = 'failed' WHERE id = ?", (task_id,))
    # 获取父订单 ID 并刷新聚合状态
    row = conn.execute("SELECT order_id FROM order_files WHERE id = ?", (task_id,)).fetchone()
    if row:
        refresh_order_status(conn, row["order_id"])
    conn.commit()
    conn.close()

    with pushed_tasks_lock:
        pushed_tasks.pop(task_id, None)


@socketio.on("page_count_result")
def on_page_count_result(data):
    """本地打印工具回报文件页数分析结果。
    data: {file_id, page_count, orientation, success}"""
    file_id = data.get("file_id", "")
    page_count = int(data.get("page_count", 0) or 0)
    orientation = data.get("orientation", "")
    success = data.get("success", True)

    if not file_id or page_count <= 0:
        print(f"  [PAGE] page_count_result 无效: file_id={file_id}, pages={page_count}")
        return

    conn = get_db()
    conn.execute(
        "UPDATE files SET page_count = ?, page_count_verified = 1 WHERE id = ?",
        (page_count, file_id),
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

    task_id = int(task_id)
    print(f"  [TRUNC] 子任务 #{task_id}: 页码范围被截断 {original} → {effective} (总 {total_pages} 页)")

    conn = get_db()
    conn.execute(
        "UPDATE order_files SET page_range_original = ?, page_range_truncated = 1, page_range = ? WHERE id = ?",
        (original, effective, task_id),
    )
    conn.commit()
    conn.close()


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
    active_clients = get_active_clients()
    if not active_clients:
        print(f"  [PAGE] 无活跃打印客户端，跳过页数分析请求")
        return False

    download_url = make_download_url(file_id)
    client_id = active_clients[0]

    with printer_clients_lock:
        client_info = printer_clients.get(client_id)
        sid = client_info["sid"] if client_info else None

    if not sid:
        return False

    try:
        socketio.emit("analyze_page_count", {
            "file_id": file_id,
            "file_name": file_name,
            "download_url": download_url,
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
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = get_db()
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


@app.route("/api/ping")
def ping():
    return {"msg": "pong", "status": "ok"}


@app.route("/api/printer_status", methods=["GET"])
def printer_status():
    """返回打印机在线状态。"""
    with printer_clients_lock:
        online_count = len(printer_clients)
    return jsonify({
        "success": True,
        "online": online_count > 0,
        "active": online_count > 0,
        "count": online_count,
        "client_count": online_count,
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


@app.route("/api/file_page/<file_id>", methods=["GET"])
@login_required
def get_file_page(file_id):
    """查询文件的页数信息（供前端轮询本地工具分析结果）。
    返回 page_count 和 page_count_verified 标志。"""
    conn = get_db()
    row = conn.execute(
        "SELECT page_count, page_count_verified FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"success": False, "message": "文件不存在"}), 404
    return jsonify({
        "success": True,
        "page_count": row["page_count"] or 0,
        "verified": bool(row["page_count_verified"]),
    })


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
    conn = get_db()
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
    """Android/Web 设备登录：用 device_id 创建或恢复账号，无需微信。"""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    device_id = (data.get("device_id") or "").strip()
    if not device_id or len(device_id) < 6:
        return jsonify({"success": False, "message": "device_id 无效"}), 400

    # 用 device_id 生成稳定的 openid
    openid = "dev_" + hashlib.sha256(device_id.encode()).hexdigest()[:24]
    token = token_serializer.dumps(openid)

    conn = get_db()
    existing = conn.execute("SELECT openid FROM users WHERE openid = ?", (openid,)).fetchone()
    if not existing:
        suffix = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        nickname = f"手机用户_{suffix}"
        conn.execute(
            "INSERT INTO users (openid, nickname, avatar_path, updated_at) VALUES (?, ?, '', ?)",
            (openid, nickname, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    conn.close()

    print(f"设备登录: device_id={device_id[:12]}..., openid={openid[:8]}...")

    return jsonify({
        "success": True,
        "token": token,
        "openid": openid,
    })


@app.route("/api/upload", methods=["POST"])
@require_printer_access
def upload_file():
    if "file" not in request.files:
        return jsonify({"success": False, "message": "未找到上传文件"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "message": "文件名为空"}), 400

    file_id = uuid.uuid4().hex
    ext = os.path.splitext(f.filename)[1]  # 保留原始大小写
    ext_lower = ext.lower().lstrip(".")
    saved_name = f"{file_id}{ext}"

    # 1. 先保存到 uploads/ 根目录作为临时文件
    temp_path = os.path.join(UPLOAD_DIR, saved_name)
    f.save(temp_path)
    file_size = os.path.getsize(temp_path)

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

    # 6. 写入 files 表（含 page_count 缓存和 md5）
    conn = get_db()
    conn.execute(
        """
        INSERT INTO files (id, original_name, saved_name, path, size, created_at, page_count, page_count_verified, md5)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, f.filename, saved_name, file_path, file_size,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), page_count,
         int(cached_page_verified), file_md5),
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
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    duplex = data.get("duplex", "on")  # 顶层 duplex 作为默认值（向后兼容）
    files_input = data.get("files", None)

    # ---- v5 新增：附加服务参数 ----
    delivery_enabled = int(data.get("delivery_enabled", 0) or 0)
    delivery_location = data.get("delivery_location", "")
    delivery_percentage = float(data.get("delivery_percentage", 0) or 0)
    urgency = data.get("urgency", "低")
    urgency_price = float(data.get("urgency_price", 0) or 0)
    cover_page = int(data.get("cover_page", 0) or 0)
    cover_page_price = float(data.get("cover_page_price", 0.15) or 0)
    pickup_address = data.get("pickup_address", "")

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
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    order_number = generate_order_number()

    # ---- 检查打印机在线状态 ----
    printer_online = len(get_active_clients()) > 0

    # ---- 事务：插 1 条 orders + N 条 order_files ----
    conn = get_db()
    try:
        # 先插父订单（聚合字段用首文件填充，后续严格通过 order_files 聚合）
        first_file_name = files_input[0].get("file", files_input[0].get("file_name", ""))
        conn.execute(
            """INSERT INTO orders (file_id, file, copies, status, created_at, openid, duplex,
                                   page_count, price_per_page, total_price, is_free,
                                   delivery_enabled, delivery_location, delivery_percentage,
                                   urgency, urgency_price, cover_page, cover_page_price, pickup_address,
                                   order_number, source)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, 1, 0, 0, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cloud')""",
            (files_input[0].get("file_id") or None, first_file_name, 0,
             created_at, g.openid, duplex,
             1 if user_is_admin else 0,
             delivery_enabled, delivery_location, delivery_percentage,
             urgency, urgency_price, cover_page, cover_page_price, pickup_address,
             order_number),
        )
        order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 插每个子任务
        sub_tasks = []
        for f in files_input:
            f_id = f.get("file_id", "") or ""
            f_name = f.get("file", f.get("file_name", ""))
            f_copies = int(f.get("copies", 1))
            f_page_range = (f.get("page_range", "") or "").strip()

            # 计算页数与价格（优先从 files 表缓存读取）
            page_count = 1
            page_count_verified = False
            if f_id:
                frow = conn.execute(
                    "SELECT path, original_name, page_count, page_count_verified FROM files WHERE id = ?",
                    (f_id,),
                ).fetchone()
                if frow:
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

            is_free_val = 1 if user_is_admin else 0
            per_copy_price = calculate_price(page_count, f_duplex)
            total_price = 0 if user_is_admin else round(per_copy_price * f_copies, 2)
            # 统一从 queued 开始，由 push_print_task_to_client 原子锁定为 printing
            sub_status = "queued"

            conn.execute(
                """INSERT INTO order_files (order_id, file_id, file_name, copies, page_count,
                                            page_range, price_per_page, total_price, is_free, status, created_at, duplex)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, f_id or None, f_name, f_copies, page_count,
                 f_page_range, 0, total_price, is_free_val, sub_status, created_at, f_duplex),
            )
            sub_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            sub_tasks.append({
                "id": sub_task_id, "file_id": f_id, "file_name": f_name,
                "copies": f_copies, "page_count": page_count,
                "page_count_verified": page_count_verified,
                "page_range": f_page_range,
                "total_price": total_price, "status": sub_status,
                "duplex": f_duplex,
            })

        # 汇总父订单 total_price 和 page_count
        parent_total_price = sum(st["total_price"] for st in sub_tasks)
        parent_page_count = sum(st["page_count"] * st["copies"] for st in sub_tasks)
        conn.execute(
            "UPDATE orders SET total_price = ?, page_count = ? WHERE id = ?",
            (parent_total_price, parent_page_count, order_id),
        )

        # 聚合初态并写入父订单
        new_status = aggregate_order_status(conn, order_id) or "queued"
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))

        # 若当前用户是临时授权用户，将订单关联到其许可密钥并清除临时授权
        urow = conn.execute(
            "SELECT temp_until FROM users WHERE openid = ?", (g.openid,),
        ).fetchone()
        if urow and urow["temp_until"] and not user_is_admin:
            conn.execute(
                "UPDATE license_keys SET order_id = ? WHERE used_by = ? AND order_id IS NULL ORDER BY id DESC LIMIT 1",
                (order_id, g.openid),
            )
            conn.execute(
                "UPDATE users SET temp_until = NULL WHERE openid = ?",
                (g.openid,),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    # ---- 推送（事务外，避免长事务） ----
    pushed_count = 0
    if printer_online:
        active_clients = get_active_clients()
        if active_clients:
            client_id = active_clients[0]
            for st in sub_tasks:
                if st["file_id"]:
                    if push_print_task_to_client(st["id"], st["file_id"], st["file_name"],
                                                  st["copies"], st.get("duplex", duplex),
                                                  st.get("page_range", ""), client_id):
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
    print(data)

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
    """打印机客户端拉取排队中的子任务（原子取锁，每次返回一个任务，防止多打印机重复领取）"""
    token = request.args.get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    client_id = request.args.get("client_id", "") or socket.gethostname()

    task = fetch_and_lock_task(client_id)
    if not task:
        return jsonify({"success": True, "orders": [], "count": 0})

    # 每文件 duplex 已存入 order_files 表，直接从 task 读取
    duplex = task.get("duplex", "on") or "on"

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
        "task_id": task["id"],               # 客户端用 task_id = order_files.id
        "options": {
            "copies": task["copies"],
            "duplex": duplex,
            "page_range": task.get("page_range", "") or "",
        },
        "delivery_enabled": bool(task.get("delivery_enabled", False)),
        "delivery_location": task.get("delivery_location", "") or "",
        "urgency": task.get("urgency", "低") or "低",
        "cover_page": bool(task.get("cover_page", False)),
        "cover_page_price": float(task.get("cover_page_price", 0.15) or 0.15),
    }
    if task["file_id"]:
        item["download_url"] = make_download_url(task["file_id"])

    return jsonify({
        "success": True,
        "orders": [item],
        "count": 1,
    })


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
    if source_filter in ("cloud", "local"):
        source_clause = " AND source = ?"
        source_params = [source_filter]

    if view_all:
        # 超级管理员查看全部订单
        where_clause = "1 = 1" + source_clause
        params = source_params.copy()
    elif is_super and target_openid:
        # 超级管理员查看指定用户
        where_clause = "openid = ?" + source_clause
        params = [target_openid] + source_params
    elif role == "admin" and target_openid:
        # 管理员查看指定用户：需验证该用户是否由本管理员授权
        auth_row = conn.execute(
            "SELECT used_by FROM license_keys WHERE created_by = ? AND used_by = ? LIMIT 1",
            (g.openid, target_openid),
        ).fetchone()
        if not auth_row:
            conn.close()
            return jsonify({"success": False, "message": "无权查看该用户的订单"}), 403
        where_clause = "openid = ?" + source_clause
        params = [target_openid] + source_params
    else:
        # 默认：所有角色只看自己的订单
        where_clause = "openid = ?" + source_clause
        params = [g.openid] + source_params

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
               urgency, urgency_price, cover_page, cover_page_price, pickup_address
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
        oid = order["id"]

        # 查询子任务
        of_rows = conn.execute(
            """SELECT id, file_id, file_name, copies, page_count, page_range,
                      page_range_original, page_range_truncated,
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
        orders.append(order)

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
               o.pickup_address
        FROM orders o
        WHERE o.id = ? AND o.openid = ?
        """,
        (order_id, g.openid),
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"success": False, "message": "任务不存在或无权访问"}), 404

    order = dict(row)

    # 查询子任务
    of_rows = conn.execute(
        """SELECT of.id, of.file_id, of.file_name, of.copies, of.page_count,
                  of.page_range, of.page_range_original, of.page_range_truncated,
                  of.total_price, of.is_free, of.status, of.duplex
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
        """SELECT of.id, of.file_name, of.copies, of.page_count, of.duplex,
                  of.total_price, of.is_free, of.status
           FROM order_files of WHERE of.order_id = ? ORDER BY of.id ASC""",
        (order_id,),
    ).fetchall()

    files = []
    for of_row in of_rows:
        f = dict(of_row)
        # 附上单价明细（用于前端展示）
        per_copy_price = calculate_price(f["page_count"], f.get("duplex", "on"))
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
    """取消任务（仅限 queued 或 printing 状态且属于当前用户）。
    取消后通过 SocketIO 通知已连接的打印机客户端。"""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    order_id = data.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT id, status, openid FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"success": False, "message": "任务不存在"}), 404

    if row["openid"] != g.openid:
        conn.close()
        return jsonify({"success": False, "message": "无权操作此任务"}), 403

    if row["status"] not in ("queued", "printing"):
        conn.close()
        return jsonify({"success": False, "message": f"任务状态为 {row['status']}，无法取消"}), 400

    # 查询被取消的子任务 ID 和已连接的打印机客户端
    sub_tasks = conn.execute(
        "SELECT id, operator_client FROM order_files WHERE order_id = ? AND status IN ('queued', 'printing')",
        (order_id,),
    ).fetchall()

    # 取消父订单和所有子任务
    conn.execute(
        "UPDATE order_files SET status = 'canceled' WHERE order_id = ? AND status IN ('queued', 'printing')",
        (order_id,),
    )
    conn.execute(
        "UPDATE orders SET status = 'canceled' WHERE id = ?",
        (order_id,),
    )
    conn.commit()
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
    """打印机确认接受订单（需 token 认证）。将状态从 printing 改为 accepted（终端状态）。"""
    token = request.args.get("token", "") or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "") or request.args.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400
    conn = get_db()
    conn.execute(
        "UPDATE order_files SET status = 'accepted' WHERE order_id = ? AND status = 'printing'",
        (order_id,),
    )
    conn.execute(
        "UPDATE orders SET status = 'accepted' WHERE id = ?",
        (order_id,),
    )
    conn.commit()
    conn.close()
    print(f"  [ACCEPT] 订单 #{order_id} 已被打印机接受")
    return jsonify({"success": True, "message": "订单已接受"})


@app.route("/api/reject_order", methods=["POST"])
def reject_order():
    """打印机打回订单（需 token 认证）。将订单状态设为 rejected。"""
    token = request.args.get("token", "") or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "") or request.args.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400

    conn = get_db()
    # 将子任务和父订单都设为 rejected
    conn.execute(
        "UPDATE order_files SET status = 'rejected' WHERE order_id = ? AND status IN ('queued', 'printing')",
        (order_id,),
    )
    conn.execute(
        "UPDATE orders SET status = 'rejected' WHERE id = ?",
        (order_id,),
    )
    conn.commit()
    conn.close()

    print(f"  [REJECT] 订单 #{order_id} 已被打印机打回")
    return jsonify({"success": True, "message": "订单已打回"})


@app.route("/api/abandon_order", methods=["POST"])
def abandon_order():
    """打印机放弃已接受的订单（需 token 认证）。"""
    token = request.args.get("token", "") or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "") or request.args.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400
    conn = get_db()
    conn.execute(
        "UPDATE order_files SET status = 'abandoned' WHERE order_id = ? AND status = 'accepted'",
        (order_id,),
    )
    conn.execute(
        "UPDATE orders SET status = 'abandoned' WHERE id = ?",
        (order_id,),
    )
    conn.commit()
    conn.close()
    print(f"  [ABANDON] 订单 #{order_id} 已被放弃打印")
    return jsonify({"success": True, "message": "订单已标记为放弃打印"})


# ==================== 本地订单上报（本地打印工具使用）====================

@app.route("/api/local_orders", methods=["POST"])
def local_orders():
    """本地打印工具上报本地打印任务。需 token 认证。"""
    token = request.args.get("token", "") or (request.get_json(silent=True) or {}).get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403

    data = request.get_json(silent=True) or {}
    order_number = data.get("order_number", "")
    files = data.get("files", [])
    total_price = data.get("total_price", 0)
    created_at = data.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    if not order_number or not files:
        return jsonify({"success": False, "message": "缺少 order_number 或 files"}), 400

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                                   total_price, source)
               VALUES (?, 1, 'sent', ?, 'local', ?, ?, 'local')""",
            (f"本地打印 {len(files)} 个文件", created_at, order_number, total_price),
        )
        order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for f_info in files:
            conn.execute(
                """INSERT INTO order_files (order_id, file_name, copies, page_count,
                                            total_price, status, duplex, created_at, page_range)
                   VALUES (?, ?, ?, ?, ?, 'sent', ?, ?, ?)""",
                (order_id, f_info.get("file_name", ""), f_info.get("copies", 1),
                 f_info.get("page_count", 0), f_info.get("cost", 0),
                 f_info.get("duplex", "on"), created_at, f_info.get("page_range", "")),
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
    """返回当前用户的 openid、角色、临时授权信息"""
    role = compute_role(g.openid)
    temp_until = None
    has_temp_access = False
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    row = conn.execute(
        "SELECT temp_until FROM users WHERE openid = ?", (g.openid,)
    ).fetchone()
    conn.close()
    if row and row["temp_until"]:
        temp_until = row["temp_until"]
        has_temp_access = row["temp_until"] > now_str
    is_super = SUPER_ADMIN_OPENID and g.openid == SUPER_ADMIN_OPENID
    return jsonify({
        "success": True,
        "openid": g.openid,
        "is_admin": role == "admin",
        "is_super_admin": bool(is_super),
        "role": role,
        "temp_until": temp_until,
        "has_temp_access": has_temp_access,
    })


# ==================== 授权用户列表（管理员查看）====================


@app.route("/api/authorized_users", methods=["GET"])
@login_required
def authorized_users():
    """管理员查看自己授权过的用户列表（含最近订单摘要）。"""
    role = compute_role(g.openid)
    if role != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    conn = get_db()
    # 查询该管理员创建的所有已使用的密钥（去重用户）
    rows = conn.execute(
        """SELECT DISTINCT lk.used_by, u.nickname,
               (SELECT MAX(o.created_at) FROM orders o WHERE o.openid = lk.used_by) AS last_order,
               (SELECT COUNT(*) FROM orders o WHERE o.openid = lk.used_by) AS order_count
           FROM license_keys lk
           LEFT JOIN users u ON lk.used_by = u.openid
           WHERE lk.created_by = ? AND lk.used_by IS NOT NULL
           ORDER BY last_order DESC NULLS LAST""",
        (g.openid,),
    ).fetchall()
    conn.close()

    users = []
    for r in rows:
        openid = r["used_by"]
        users.append({
            "openid": openid,
            "openid_short": openid[:8] + "..." if openid else "",
            "nickname": r["nickname"] or "",
            "last_order": r["last_order"] or "",
            "order_count": r["order_count"] or 0,
        })

    return jsonify({"success": True, "users": users, "count": len(users)})


# ==================== 用户资料接口 ====================


@app.route("/api/profile", methods=["GET"])
@login_required
def get_profile():
    """获取当前用户的头像和昵称"""
    conn = get_db()
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
        ext = os.path.splitext(avatar_file.filename)[1] or ".jpg"
        saved_name = f"{g.openid}{ext}"
        file_path = os.path.join(AVATAR_DIR, saved_name)
        avatar_file.save(file_path)
        avatar_path = file_path

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
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

    conn = get_db()
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
    if not is_admin(g.openid):
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

    conn = get_db()
    # 生成新密钥前，作废该管理员所有未使用的旧密钥（直接删除）
    conn.execute(
        "DELETE FROM license_keys WHERE created_by = ? AND used_by IS NULL",
        (g.openid,),
    )
    # 生成唯一密钥（避免与数据库中未失效的密钥冲突）
    for _ in range(20):
        license_key = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        dup = conn.execute(
            "SELECT id FROM license_keys WHERE key = ? AND expires_at > ?",
            (license_key, now_str),
        ).fetchone()
        if not dup:
            break

    # admin 类型永久有效，temp 类型限时 1-10 分钟
    if key_type == "admin":
        validity = 52560000  # 100 年（永久）
        expires = now + timedelta(minutes=validity)
    else:
        validity = int(data.get("validity_minutes", 5))
        validity = max(1, min(10, validity))
        expires = now + timedelta(minutes=validity)
    conn.execute(
        """INSERT INTO license_keys (key, created_by, validity_minutes, created_at, expires_at, type)
           VALUES (?, ?, ?, ?, ?, ?)""",
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
    data = request.get_json() or {}
    raw_key = (data.get("key", "") or "").strip().upper()
    if len(raw_key) != 8:
        return jsonify({"success": False, "message": "密钥格式不正确"}), 400

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    # 原子条件 UPDATE：仅当未使用且未过期才生效
    conn.execute(
        """UPDATE license_keys SET used_by = ?, used_at = ?
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
        # admin 类型: 设为管理员，清除临时授权
        if existing:
            conn.execute(
                "UPDATE users SET role = 'admin', temp_until = NULL, updated_at = ? WHERE openid = ?",
                (now_str, g.openid),
            )
        else:
            conn.execute(
                "INSERT INTO users (openid, role, temp_until, nickname, avatar_path, updated_at) VALUES (?, 'admin', NULL, '', '', ?)",
                (g.openid, now_str),
            )
    else:
        # temp 类型: 设置临时授权截止时间
        if existing:
            conn.execute(
                "UPDATE users SET temp_until = ?, updated_at = ? WHERE openid = ?",
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
    """查询当前管理员最新未过期的许可密钥及关联用户信息。
    状态: unused（未兑换）/ used_waiting（已兑换等待提交任务）/ used_done（已提交任务）
    """
    if not is_admin(g.openid):
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    row = conn.execute(
        """SELECT id, key, type, used_by, order_id, validity_minutes, created_at, expires_at
           FROM license_keys
           WHERE created_by = ? AND expires_at > ?
           ORDER BY id DESC LIMIT 1""",
        (g.openid, now_str),
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"success": True, "active": False})

    result = {
        "success": True,
        "active": True,
        "key": row["key"],
        "type": row.get("type", "temp"),
        "expires_at": row["expires_at"],
        "validity_minutes": row["validity_minutes"],
        "created_at": row["created_at"],
        "used_by": row.get("used_by") or None,
        "order_id": row.get("order_id") or None,
    }

    used_by = row.get("used_by")
    if used_by:
        # 查询兑换用户的昵称和头像
        urow = conn.execute(
            "SELECT nickname, avatar_path FROM users WHERE openid = ?", (used_by,)
        ).fetchone()
        if urow:
            result["used_by_nickname"] = urow.get("nickname") or "微信用户"
            result["used_by_avatar_url"] = get_avatar_url(used_by, urow.get("avatar_path") or "")
        else:
            result["used_by_nickname"] = "微信用户"
            result["used_by_avatar_url"] = ""

        # 查询该用户是否有订单（优先使用 license_keys.order_id 关联的订单）
        license_order_id = row.get("order_id")
        if license_order_id:
            orow = conn.execute(
                """SELECT id, status, total_price
                   FROM orders WHERE id = ?""",
                (license_order_id,),
            ).fetchone()
        else:
            orow = conn.execute(
                """SELECT id, status, total_price
                   FROM orders WHERE openid = ? ORDER BY id DESC LIMIT 1""",
                (used_by,),
            ).fetchone()
        if orow:
            result["order_id"] = orow["id"]
            result["order_status"] = orow["status"]
            result["order_total_price"] = orow.get("total_price", 0)
            result["status"] = "used_done"
        else:
            result["status"] = "used_waiting"
            result["order_id"] = None
    else:
        result["status"] = "unused"

    conn.close()
    return jsonify(result)


@app.route("/api/license/revoke", methods=["POST"])
@login_required
def license_revoke():
    """管理员作废自己当前未使用的许可密钥"""
    if not is_admin(g.openid):
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    conn = get_db()
    conn.execute(
        "DELETE FROM license_keys WHERE created_by = ? AND used_by IS NULL",
        (g.openid,),
    )
    conn.commit()
    conn.close()

    print(f"管理员 {g.openid[:8]}... 作废了当前许可密钥")
    return jsonify({"success": True})


@app.route("/api/license/finish", methods=["POST"])
@login_required
def license_finish():
    """管理员结束打印任务：查询许可证关联的订单价格详情，标记许可证为已完成。
    body: { "key": "ABCD1234" }
    """
    if not is_admin(g.openid):
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    data = request.get_json() or {}
    raw_key = (data.get("key", "") or "").strip().upper()

    if len(raw_key) != 8:
        return jsonify({"success": False, "message": "密钥格式不正确"}), 400

    conn = get_db()
    lrow = conn.execute(
        "SELECT id, key, used_by, order_id FROM license_keys WHERE key = ? AND created_by = ?",
        (raw_key, g.openid),
    ).fetchone()

    if not lrow:
        conn.close()
        return jsonify({"success": False, "message": "密钥不存在或不属于您"}), 404

    order_id = lrow.get("order_id")
    price_detail = None

    if order_id:
        # 查询订单价格明细
        orow = conn.execute(
            "SELECT id, total_price, is_free FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if orow:
            order = dict(orow)
            of_rows = conn.execute(
                """SELECT of.id, of.file_name, of.copies, of.page_count, of.duplex,
                          of.total_price, of.is_free, of.status
                   FROM order_files of WHERE of.order_id = ? ORDER BY of.id ASC""",
                (order_id,),
            ).fetchall()
            files = []
            for of_row in of_rows:
                f = dict(of_row)
                f["per_copy_price"] = calculate_price(f["page_count"], f.get("duplex", "on"))
                f["unit"] = "元/张" if f.get("duplex") == "on" else "元/页"
                files.append(f)
            price_detail = {
                "order_id": order["id"],
                "is_free": bool(order.get("is_free", 0)),
                "total_price": sum(f.get("total_price", 0) for f in files),
                "files": files,
            }

    # 删除该许可密钥（标记为已完成）
    conn.execute(
        "DELETE FROM license_keys WHERE id = ?",
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
    """清理已过期且未使用的许可密钥（超过 1 小时）"""
    cutoff = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute("DELETE FROM license_keys WHERE used_by IS NULL AND expires_at < ?", (cutoff,))
    conn.commit()
    conn.close()


@app.route("/api/admin/users", methods=["GET"])
@login_required
def admin_users_list():
    """管理员查看所有普通用户列表（含头像、昵称、许可时间）"""
    if not is_admin(g.openid):
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    conn = get_db()
    rows = conn.execute(
        """
        SELECT u.openid, u.nickname, u.avatar_path,
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

    conn = get_db()
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
    """超级管理员移除某个管理员（将其 role 改为 guest，清除 temp_until）。
    不能移除超级管理员自己。
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
    conn = get_db()
    existing = conn.execute(
        "SELECT role FROM users WHERE openid = ? AND role = 'admin'",
        (target_openid,),
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"success": False, "message": "用户不是管理员或不存在"}), 404

    conn.execute(
        "UPDATE users SET role = 'guest', temp_until = NULL, updated_at = ? WHERE openid = ?",
        (now_str, target_openid),
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
    if not is_admin(g.openid):
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
                if fname in ("md5_index.json", "retention_config.json"):
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
            if fname in ("md5_index.json", "retention_config.json"):
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


@app.route("/api/log/report", methods=["POST"])
def log_report():
    """前端上报错误/警告日志。"""
    data = request.get_json(silent=True) or {}
    level = data.get("level", "warning")
    message = data.get("message", "")
    if not message:
        return jsonify({"success": False, "message": "缺少日志内容"}), 400
    if level in ("error", "critical"):
        _frontend_log.error(message)
    else:
        _frontend_log.warning(message)
    return jsonify({"success": True})


@app.route("/api/log/fetch", methods=["GET"])
def log_fetch():
    """本地打印工具拉取后端/前端日志（需 token 认证）。"""
    token = request.args.get("token", "")
    if not PRINTER_TOKEN or token != PRINTER_TOKEN:
        return jsonify({"success": False, "message": "token 无效"}), 403
    log_type = request.args.get("type", "server")
    if log_type not in ("server", "frontend"):
        return jsonify({"success": False, "message": "type 只能为 server 或 frontend"}), 400
    log_path = os.path.join(LOG_DIR, f"{log_type}.log")
    if not os.path.exists(log_path):
        return jsonify({"success": True, "size": 0, "content": ""})
    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()
    return jsonify({"success": True, "size": len(content.encode("utf-8")), "content": content})


# ==================== 启动 ====================

if __name__ == "__main__":
    init_db()
    print("数据库已初始化")

    scheduler.add_job(process_pending_orders, "interval", seconds=30, id="scan_orders")
    scheduler.add_job(check_printing_timeout, "interval", seconds=60, id="check_timeout")
    scheduler.add_job(cleanup_expired_license_keys, "interval", minutes=10, id="cleanup_licenses")
    scheduler.add_job(cleanup_expired_files, "interval", minutes=10, id="cleanup_files")
    scheduler.add_job(recover_orphaned_printing_tasks, "interval", minutes=2, id="recover_orphans")
    scheduler.start()
    print("定时扫描已启动（任务扫描每 30s，超时检查每 60s，密钥清理每 10min）")

    socketio.run(app, host="127.0.0.1", port=5000,
                 debug=True, use_reloader=False,
                 allow_unsafe_werkzeug=True)
