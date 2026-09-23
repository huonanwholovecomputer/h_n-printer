"""
Gunicorn 配置文件 — HN 云打印后端
使用 gthread worker（与 Flask-SocketIO async_mode="threading" 匹配）
"""

# 绑定地址和端口（仅本地访问，由 Nginx 反向代理）
bind = "127.0.0.1:5000"

# gthread worker：线程模型，与 app.py 的 async_mode="threading" 配套
# （2026-09 起；此前用 eventlet，与 threading 模式不匹配，见文件末尾说明）
worker_class = "gthread"

# worker 数量必须为 1：printer_clients / pushed_tasks / _last_page_analysis_push 等
# 都是进程内内存结构，多进程会让「在线设备」「推送去重」互相看不见。
workers = 1

# 线程数：每个 WebSocket 连接会占用一个线程直到断开（threading 模式下 WebSocket
# 由 simple-websocket 在线程里收发），另外还要留给 HTTP 请求、长轮询、日志收集
# （收集流程最多占用一个线程 ~10s）与页数分析回报。按「打印机数 + 余量」配置：
# 当前 2 台设备，16 线程有充足余量；设备数接近线程数时需同步调大。
threads = 16

# 日志
accesslog = "-"          # 访问日志输出到 stdout
errorlog = "-"           # 错误日志输出到 stderr
loglevel = "info"

# 进程命名
proc_name = "printer-backend"

# 优雅重启
graceful_timeout = 30

# 保持连接
keepalive = 5


# ── worker 与 async_mode 的匹配（P2-6，2026-09-23 已修）──
# app.py 里 socketio = SocketIO(app, ..., async_mode="threading")，threading 模式用
# 标准线程处理请求，官方要求配 gthread worker。此前配的是 eventlet worker，属混用：
# 事件循环不匹配，长期运行表现为 WebSocket 偶发断连 —— 现场证据（2026-09-22）：
#   · 客户端已断开 48~57 分钟后服务端才记到 [LINK] 已断开（19:59:48 vs 19:11:58；
#     22:58:34 vs 客户端 22:01:46 的正常退出），即服务端迟迟发现不了死连接；
#   · 客户端侧多次 SSL 握手 EOF / namespace 握手超时（连接建立与数据面脱节）。
# 另一条路（未采用）：改 async_mode="eventlet" + import 前 eventlet.monkey_patch()
# —— 会改变全局语义，风险更高。


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
