"""
Gunicorn 配置文件 — HN 云打印后端

运行时有两种模式，由**同一个环境变量** `HN_RUNTIME` 决定（app.py 与这里必须一致）：
  · 不设 / 其它值  → gthread + threads=16（默认；2026-09 起一直在用）
  · `HN_RUNTIME=gevent` → gevent worker + async_mode="gevent"（S3-01，2026-10-24）
      一条 WebSocket 连接 = 一个 greenlet，不再独占 OS 线程 ⇒ 16 条以上设备连接时
      HTTP 仍可用（threading 模式 15 条就把 16 个 worker 占满，S2-12 实测）。
      切换/回滚只改这一个环境变量 + 重启（systemd Environment=），无代码分支残留。
"""
import os

_RUNTIME = (os.environ.get("HN_RUNTIME", "") or "").strip().lower()
_GEVENT = _RUNTIME == "gevent"

# 绑定地址和端口（仅本地访问，由 Nginx 反向代理）
bind = "127.0.0.1:5000"

# S3-01：worker 类型跟随运行时。gevent worker 会自己 monkey-patch
# （gunicorn/workers/ggevent.py: patch()），app.py 顶部也有一段等价的兜底
# （直跑 / 测试时用），两处都幂等。
worker_class = "gevent" if _GEVENT else "gthread"

# worker 数量必须为 1：printer_clients / pushed_tasks / _last_page_analysis_push 等
# 都是进程内内存结构，多进程会让「在线设备」「推送去重」互相看不见。
# ⚠ S3-01 的后半段（workers>1）需要先把这几样迁到 Redis，本次**没有做**；
#   在完成迁移前，任何情况下都不要把这里改成 >1（包括 gevent 模式）。
workers = 1

# 线程数（仅 gthread 生效）：每个 WebSocket 连接会占用一个线程直到断开，另外还要留给
# HTTP 请求、长轮询、日志收集与页数分析回报。按「打印机数 + 余量」配置：
# §11.2 required_threads = ceil(在线设备×2.5)+4 ⇒ 3 台 12 / 6 台 19 / 10 台 29。
# 16 只够 3 台（S2-12 实测：6 台档吞吐 −37%，10 台档连接建不起来）。
threads = 16

# gevent 模式下每个 worker 的连接上限（gunicorn 默认 1000；真实上限还受 nginx
# limit_conn 与 §11.2 的容量结论约束）。
worker_connections = 1000

# 日志
accesslog = "-"          # 访问日志输出到 stdout
# ⚠ S0-06（2026-10-23 安全加固）：**不要**用默认 access_log_format。
#   默认格式含 %(r)s（完整请求行，带 ?token=…）⇒ journald 里每台设备每次心跳都留一份
#   printer token 明文 —— DS-02 说的「日志分发面」正是这里。改成只记方法 + 路径（%(m)s %(U)s），
#   丢掉 query string；S1-01 把 token 全部搬到 header 之后可再恢复字段完整度。
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(f)s" "%(a)s"'
errorlog = "-"           # 错误日志输出到 stderr
loglevel = "info"

# 进程命名
proc_name = "printer-backend"

# 优雅重启
graceful_timeout = 30

# 保持连接
keepalive = 5


# ── worker 与 async_mode 的匹配（P2-6，2026-09-23 已修；S3-01 扩展为双模式）──
# 两条**各自自洽**的组合，靠同一个 HN_RUNTIME 一起切换：
#   · gthread  ↔ async_mode="threading"（默认）
#   · gevent   ↔ async_mode="gevent"   （S3-01，一条连接一个 greenlet）
# **禁止交叉**：2026-09 曾用 eventlet worker 配 async_mode="threading"，表现为
# WebSocket 偶发断连且服务端迟迟发现不了死连接（现场：客户端已断开 48~57 分钟后才记
# [LINK] 已断开；客户端多次 SSL 握手 EOF / namespace 握手超时）。S2-07 已把 eventlet
# 从依赖里删掉，本仓不再有 eventlet 路径。


def post_worker_init(worker):
    """
    Gunicorn worker 启动后，初始化数据库迁移和 APScheduler 定时任务。
    放在这里而不是 if __name__ 里，确保 Gunicorn 模式下也能运行。
    """
    from app import (
        init_db,
        scheduler,
        process_pending_orders,
        process_scheduled_orders,
        check_printing_timeout,
        cleanup_expired_files,
        cleanup_expired_license_keys,
        recover_orphaned_printing_tasks,
        recover_stale_downloading,
        cleanup_abandoned_reserved_orders,
        recover_stale_printing_tasks,
        expire_stale_queued_orders,
    )

    # 先执行数据库初始化/迁移（生产环境 Gunicorn 不会触发 __main__）
    init_db()
    print("[DB] 数据库初始化/迁移完成")

    # P2-16：启动时立即清理崩溃残留的 printing 任务（pushed_tasks 是内存结构，
    # 进程重启后丢失；不重置的话上次进程的 printing 会永久卡住，直到 5 分钟孤儿回收）
    recover_stale_printing_tasks()
    print("[RECOVER] 启动时 printing 残留检查完成")

    # 幽灵文件清理：物理已清理/路径已清空的 files 记录 → 删除。
    # 存量幽灵若不清理，会在每次打印机上线补推页数分析时反复推送已清理文件（下载 404 刷屏）。
    # cleanup_expired_files 内部含幽灵清理（不受保留期限制），启动时执行一次立即生效。
    cleanup_expired_files()
    print("[CLEANUP] 启动时幽灵文件记录清理完成")

    def _ensure_job(job_id, fn, **kwargs):
        """按 id 幂等注册：避免 reload / 直跑模式下重复添加"""
        if not scheduler.get_job(job_id):
            scheduler.add_job(fn, "interval", id=job_id, **kwargs)

    # 原有 5 个任务（仅在未注册时添加，防止 reload/直跑重复）
    if not scheduler.get_job("scan_orders"):
        _ensure_job("scan_orders", process_pending_orders, seconds=30)
        _ensure_job("check_timeout", check_printing_timeout, seconds=60)
        _ensure_job("cleanup_licenses", cleanup_expired_license_keys, minutes=10)
        _ensure_job("cleanup_files", cleanup_expired_files, minutes=10)
        _ensure_job("recover_orphans", recover_orphaned_printing_tasks, minutes=2)
    # P1-1：补齐缺失的 3 个定时任务（放在所有注册之外，确保不被上面的早退逻辑跳过）
    _ensure_job("scan_scheduled", process_scheduled_orders, seconds=30)
    _ensure_job("recover_stale_downloads", recover_stale_downloading, minutes=2)
    _ensure_job("cleanup_reserved", cleanup_abandoned_reserved_orders, minutes=5)
    # 防滥用：排队超时淘汰（阈值由 /api/admin/security 调整）
    _ensure_job("expire_stale_queued", expire_stale_queued_orders, minutes=10)

    try:
        scheduler.start()
    except Exception as e:
        print(f"[SCHEDULER] 启动失败（可能已在直跑模式下启动）: {e}")
    print("[SCHEDULER] 定时任务已启动（任务扫描30s, 预约扫描30s, 超时60s, 清理10min, 孤儿恢复2min, 预留清理5min）")
