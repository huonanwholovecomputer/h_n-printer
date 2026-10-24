"""00 · 建隔离副本 %TEMP%\\ddos_audit_copy（排除 __pycache__、logs/uploads 内容）
并把 config.py 换成假值（不复制真实密钥）。

用法（在任意目录）：
  PYTHONPATH= PYTHONUTF8=1 "C:/Users/.../python.exe" tests/staging/00_make_copy.py

幂等：每次运行先删旧副本再重建。
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BACKEND, COPY  # noqa: E402

FAKE_CONFIG = '''# 审计副本专用假配置（由 00_make_copy.py 生成，非生产值）
WECHAT_APPID = "wxFAKEAUDITAPPID0001"
WECHAT_APPSECRET = "FAKEAUDITSECRET00000000000000000000"
SECRET_KEY = "audit-fake-secret-key-0123456789abcdef"
ADMIN_OPENIDS = ["fake_admin_openid"]
SUPER_ADMIN_OPENID = "fake_admin_openid"
PUBLIC_BASE_URL = "http://127.0.0.1:18080"
CLOUD_API_URL = "http://127.0.0.1:18080"
WEBSOCKET_URL = "http://127.0.0.1:18080"
TOKEN = "fake-printer-token-for-audit"
PRINTER_NAME = ""
'''


def main():
    if not os.path.isdir(BACKEND):
        sys.exit(f"源目录不存在: {BACKEND}")
    if os.path.isdir(COPY):
        shutil.rmtree(COPY, ignore_errors=True)
    os.makedirs(COPY, exist_ok=True)
    copied = 0
    skipped = 0
    for root, dirs, files in os.walk(BACKEND):
        rel = os.path.relpath(root, BACKEND)
        parts = [] if rel == "." else rel.split(os.sep)
        # 排除 __pycache__ / logs 内容 / uploads 内容（目录保留，内容清空）
        if "__pycache__" in parts or (parts and parts[0] in ("logs", "uploads")):
            skipped += 1
            continue
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        dst_root = os.path.join(COPY, rel) if parts else COPY
        os.makedirs(dst_root, exist_ok=True)
        if not parts:
            os.makedirs(os.path.join(COPY, "logs"), exist_ok=True)
            os.makedirs(os.path.join(COPY, "uploads"), exist_ok=True)
        for fn in files:
            if fn.endswith(".pyc"):
                skipped += 1
                continue
            src = os.path.join(root, fn)
            dst = os.path.join(dst_root, fn)
            if fn == "config.py":
                continue  # 用假配置覆盖
            shutil.copy2(src, dst)
            copied += 1
    with open(os.path.join(COPY, "config.py"), "w", encoding="utf-8") as f:
        f.write(FAKE_CONFIG)
    print(f"[COPY] 源   : {BACKEND}")
    print(f"[COPY] 目标 : {COPY}")
    print(f"[COPY] 文件 : {copied} 个，跳过 {skipped} 项")
    print(f"[COPY] uploads 条目: {os.listdir(os.path.join(COPY, 'uploads'))}")
    print(f"[COPY] logs 条目   : {os.listdir(os.path.join(COPY, 'logs'))}")
    for db in ("orders.db", "users.db"):
        p = os.path.join(COPY, db)
        print(f"[COPY] {db}: {'存在' if os.path.exists(p) else '缺失'} "
              f"{os.path.getsize(p) if os.path.exists(p) else 0} bytes")
    conf = open(os.path.join(COPY, "config.py"), encoding="utf-8").read()
    assert "FAKEAUDIT" in conf and "FAKE" in conf
    print("[COPY] config.py 已替换为假值（无生产密钥）")


if __name__ == "__main__":
    main()
