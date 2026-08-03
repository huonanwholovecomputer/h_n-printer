"""
Gunicorn 配置文件 — HN 云打印后端
使用 eventlet worker 以支持 WebSocket（Flask-SocketIO）
"""

# 绑定地址和端口（仅本地访问，由 Nginx 反向代理）
bind = "127.0.0.1:5000"

# eventlet worker：支持异步 WebSocket 长连接
worker_class = "eventlet"

# worker 数量（单 worker 即可，eventlet 是协程模型）
workers = 1

# 每个 worker 的线程数（eventlet 下通常为 1）
threads = 1

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


# ── eventlet 与 Flask-SocketIO async_mode 不匹配说明（P2-6，保守处理，不改运行时行为）──
# app.py 中 socketio = SocketIO(app, ..., async_mode="threading")，threading 模式使用
# werkzeug 标准线程处理请求；而本配置用 eventlet worker。Flask-SocketIO 官方要求
# 两种模式匹配：eventlet 模式需在 import 前 eventlet.monkey_patch() 并指定
# async_mode="eventlet"；threading 模式则需 gthread worker。当前混用存在事件循环
# 不匹配风险（WebSocket 由 eventlet 协程调度、HTTP 走 threading）。
# 备选方案（二选一，需配套改造）：
#   1. worker_class = "gthread"（与 async_mode="threading" 匹配，无需 monkey_patch）；
#   2. 改 app.py 为 async_mode="eventlet"，并在 import socketio 之前
#      import eventlet; eventlet.monkey_patch()（会改变全局语义，风险高，需回归测试）。
# 当前保持 threading + eventlet worker 组合；生产长期运行若出现 WebSocket 偶发断连，
# 优先切换到方案 1（gthread）。


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
    )

    # 先执行数据库初始化/迁移（生产环境 Gunicorn 不会触发 __main__）
    init_db()
    print("[DB] 数据库初始化/迁移完成")

    # P2-16：启动时立即清理崩溃残留的 printing 任务（pushed_tasks 是内存结构，
    # 进程重启后丢失；不重置的话上次进程的 printing 会永久卡住，直到 5 分钟孤儿回收）
    recover_stale_printing_tasks()
    print("[RECOVER] 启动时 printing 残留检查完成")

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

    try:
        scheduler.start()
    except Exception as e:
        print(f"[SCHEDULER] 启动失败（可能已在直跑模式下启动）: {e}")
    print("[SCHEDULER] 定时任务已启动（任务扫描30s, 预约扫描30s, 超时60s, 清理10min, 孤儿恢复2min, 预留清理5min）")
