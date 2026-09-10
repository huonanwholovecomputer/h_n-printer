# -*- coding: utf-8 -*-
"""验收脚本 · 云端任务下载文件暂存化（同类隐患：%TEMP% 临时文件消失）

云端任务原先下载到 %TEMP%\\hn_cloud_*：临时目录被磁盘清理/安全软件清理、系统重启后
文件消失，而预约打印可能数小时后才到点；旧版「启动清理 24h 以上的 hn_cloud_*」也会把
仍在列表里的任务文件删掉 → 打印时「文件不存在」。

修复后：下载目标改为 %APPDATA%\\HN打印工具\\file_cache\\cloud_<task_id>\\<原文件名>，
下载期间用 staging.reserve() 标记「使用中」防止刚下好就被清扫，落地后由 GUI 的按引用
清扫回收。

本脚本用真实 CloudClient._download_file_inner + 伪造 HTTP 响应跑行为（无网络）：
    python tests/verify_cloud_download_staging.py      # exit 0 = 全绿
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_cloud_dl_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths                       # noqa: E402
import staging                     # noqa: E402
import cloud_client as cc          # noqa: E402
from cloud_client import CloudClient, CloudTask  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class _FakeResp:
    """伪造 requests 的流式响应。"""
    def __init__(self, chunks, headers=None, fail=False):
        self.headers = headers or {}
        self._chunks = chunks
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("HTTP 500")

    def iter_content(self, chunk_size=65536):
        for c in self._chunks:
            yield c


def _make_client():
    return CloudClient(api_url="http://127.0.0.1:1", ws_url="", token="t",
                       client_id="staging-test", parent=None)


print("=== 1. 下载成功 → 文件落在暂存目录 ===")
client = _make_client()
task = CloudTask({"task_id": 4242, "order_id": 7, "file_name": "云端订单.pdf",
                  "download_url": "http://example.invalid/f"})
client._pending_tasks[4242] = task            # 幽灵任务检查要求任务仍在待处理列表
payload = b"%PDF-1.4 cloud file " * 50
cc.http_requests.get = lambda url, **kw: _FakeResp(
    [payload], headers={"Content-Disposition": 'attachment; filename="云端订单.pdf"',
                        "content-length": str(len(payload))})
client._download_semaphore = __import__("threading").BoundedSemaphore(4)

client._download_file_inner(task)
check("任务状态 ready", task.status == "ready", task.status)
check("local_path 指向暂存目录", staging.is_staged(task.local_path), task.local_path)
check("落在 paths.file_cache_dir() 之内",
      os.path.normcase(os.path.dirname(os.path.dirname(task.local_path)))
      == os.path.normcase(paths.file_cache_dir()))
check("按 task_id 分目录", os.path.basename(os.path.dirname(task.local_path)) == "cloud_4242")
check("保留后端原始文件名（打印/报表显示名不变）",
      os.path.basename(task.local_path) == "云端订单.pdf")
with open(task.local_path, "rb") as f:
    check("下载内容完整", f.read() == payload)
check("下载结束后解除「使用中」标记", staging._reserved_snapshot() == set(),
      str(staging._reserved_snapshot()))

print("=== 2. 待确认（未挂到列表任务）期间不被清扫误删 ===")
check("被任务引用 → 清扫保留", staging.sweep({task.local_path}) == 0
      and os.path.isfile(task.local_path))
check("打回/取消后无人引用 → 清扫回收",
      staging.sweep(set()) == 1 and not os.path.isfile(task.local_path))

print("=== 3. 模拟「下载中触发清扫」的竞态窗口 ===")
task2 = CloudTask({"task_id": 4243, "order_id": 7, "file_name": "预约单.docx",
                   "download_url": "http://example.invalid/f2"})
client._pending_tasks[4243] = task2
_captured = {}
_orig_reserve = staging.reserve


def _reserve_and_sweep(p):
    """下载开始（reserve）后立刻模拟一次用户触发的清扫。"""
    _orig_reserve(p)
    _captured["swept"] = staging.sweep(set())


staging.reserve = _reserve_and_sweep
try:
    client._download_file_inner(task2)
finally:
    staging.reserve = _orig_reserve
check("下载开始后立刻清扫：文件未被删",
      _captured.get("swept") == 0 and task2.status == "ready"
      and os.path.isfile(task2.local_path), f"swept={_captured.get('swept')} status={task2.status}")

print("=== 4. 下载失败 → 不残留半截文件、不残留「使用中」标记 ===")
task3 = CloudTask({"task_id": 4244, "order_id": 7, "file_name": "失败.pdf",
                   "download_url": "http://example.invalid/f3"})
client._pending_tasks[4244] = task3
_fail_dest = staging.cloud_download_path(4244, "失败.pdf")
cc.http_requests.get = lambda url, **kw: _FakeResp([b"x"], fail=True)
client._download_file_inner(task3)
check("任务标记 error", task3.status == "error", task3.status)
check("失败路径不残留文件", not os.path.isfile(_fail_dest))
check("失败后解除「使用中」标记", staging._reserved_snapshot() == set())

print("=== 5. 幽灵任务（下载期间被取消）→ 文件丢弃 ===")
task4 = CloudTask({"task_id": 4245, "order_id": 7, "file_name": "取消.pdf",
                   "download_url": "http://example.invalid/f4"})
# 不放入 _pending_tasks：模拟下载期间任务已被取消/打回
cc.http_requests.get = lambda url, **kw: _FakeResp(
    [b"%PDF-1.4 cancelled"], headers={"Content-Disposition": 'filename="取消.pdf"'})
client._download_file_inner(task4)
_ghost_dest = staging.cloud_download_path(4245, "取消.pdf")
check("幽灵任务文件被丢弃", not os.path.isfile(_ghost_dest))
check("幽灵任务未设置 local_path", not task4.local_path)

print("=== 6. MD5 校验失败重试（暂存路径不变，二次下载覆盖）===")
_GOOD = b"%PDF-1.4 good"
import hashlib as _hashlib  # noqa: E402

task5 = CloudTask({"task_id": 4246, "order_id": 7, "file_name": "校验.pdf",
                   "download_url": "http://example.invalid/f5",
                   "source_md5": _hashlib.md5(_GOOD).hexdigest()})
client._pending_tasks[4246] = task5
_attempts = {"n": 0}


def _get_retry(url, **kw):
    _attempts["n"] += 1
    body = _GOOD if _attempts["n"] > 1 else b"%PDF-1.4 bad"   # 第 1 次故意下错内容
    return _FakeResp([body], headers={"Content-Disposition": 'filename="校验.pdf"'})


cc.http_requests.get = _get_retry
client._download_file_inner(task5)
check("重试后成功且只尝试两次", task5.status == "ready" and _attempts["n"] == 2,
      f"status={task5.status} attempts={_attempts['n']}")
check("重试后文件位于暂存目录且内容为新下载", staging.is_staged(task5.local_path)
      and open(task5.local_path, "rb").read() == _GOOD)
check("重试后无 .part 残留",
      not any(n.endswith(".part") for _d, _s, fs in os.walk(paths.file_cache_dir()) for n in fs))

print("=== 7. 暂存目录不可用 → 回退系统临时目录（功能不退化）===")
task6 = CloudTask({"task_id": 4247, "order_id": 7, "file_name": "回退.pdf",
                   "download_url": "http://example.invalid/f6"})
client._pending_tasks[4247] = task6
_orig_dl_path = staging.cloud_download_path


def _boom(*a, **k):
    raise OSError("模拟暂存目录不可用")


staging.cloud_download_path = _boom
cc.http_requests.get = lambda url, **kw: _FakeResp(
    [b"%PDF-1.4 fallback"], headers={"Content-Disposition": 'filename="回退.pdf"'})
try:
    client._download_file_inner(task6)
finally:
    staging.cloud_download_path = _orig_dl_path
check("回退到 %TEMP% 且下载成功", task6.status == "ready" and os.path.isfile(task6.local_path),
      task6.local_path)
check("回退文件名仍是旧版 hn_cloud_ 前缀（旧清理逻辑兜得住）",
      os.path.basename(task6.local_path).startswith("hn_cloud_4247_"))
try:
    os.remove(task6.local_path)
except OSError:
    pass

print("=== 8. 收尾 ===")
staging.sweep(set())
check("暂存目录无残留", staging.cache_size()[0] == 0, str(staging.cache_size()))

shutil.rmtree(_SANDBOX, ignore_errors=True)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
