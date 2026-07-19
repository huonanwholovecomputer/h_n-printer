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


def post_worker_init(worker):
    """
    Gunicorn worker 启动后，初始化 APScheduler 定时任务。
    放在这里而不是 if __name__ 里，确保 Gunicorn 模式下也能运行。
    """
    from app import (
        scheduler,
        process_pending_orders,
        check_printing_timeout,
        cleanup_expired_files,
        cleanup_expired_license_keys,
        recover_orphaned_printing_tasks,
    )

    # 避免 reload 时重复添加
    if scheduler.get_job("scan_orders"):
        return

    scheduler.add_job(process_pending_orders, "interval", seconds=30, id="scan_orders")
    scheduler.add_job(check_printing_timeout, "interval", seconds=60, id="check_timeout")
    scheduler.add_job(cleanup_expired_license_keys, "interval", minutes=10, id="cleanup_licenses")
    scheduler.add_job(cleanup_expired_files, "interval", minutes=10, id="cleanup_files")
    scheduler.add_job(recover_orphaned_printing_tasks, "interval", minutes=2, id="recover_orphans")
    scheduler.start()
    print("[SCHEDULER] 定时任务已启动（任务扫描30s, 超时60s, 清理10min, 孤儿恢复2min）")
