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
