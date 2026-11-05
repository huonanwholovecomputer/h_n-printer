# -*- coding: utf-8 -*-
"""验收脚本 · 网络抖动下的健壮性（2026-11-04 现场：下载一次失败就判死 + -L 单静默丢失 + 崩溃无栈）

用户报告（2026-11-04）：
1. 早上第一个任务「云端关闭了一个远程连接」，随后**下载直接判死**（一次重试都没有）；
2. 一个打印中的本地订单把**整个程序崩掉**（原生层，日志与系统都没留栈）；
3. 十几个订单突然都变成本地号（HN20261104-L0006 / L0007…），事后云端没有这些单。

本脚本验收三块改动：
  A. 下载：「瞬时网络错误」退避重试（旧代码只重试 MD5 不一致，网络一抖任务就 error）；
     4xx 业务拒绝**不**重试。
  B. 取号：失败不再静默（记 HTTP 状态码/正文）；任何 -L 兜底都先落**本地离线库**，
     这样即使操作员删掉已完成的标签页，这单仍能在联网后换号补报（L0006~L0009 就是这么丢的）。
  C. 崩溃取证：faulthandler（致命信号 → 全线程 Python 栈）+ 线程 excepthook；
     GDI 打印段补 logger 面包屑（原来只有 print()，打包版 console=False ⇒ 全进黑洞）。

    python other/tests/verify_network_resilience.py      # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile
import threading
import time
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_netres_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import requests as _real_requests                  # noqa: E402
import paths                                       # noqa: E402
import staging                                     # noqa: E402
import cloud_client as cc                          # noqa: E402
from cloud_client import CloudClient, CloudTask    # noqa: E402

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    if not cond:
        ok_all = False
        print(f"[FAIL] {name}" + (f"  -- {extra}" if extra else ""))
    else:
        print(f"[PASS] {name}" + (f"  -- {extra}" if extra else ""))


# ══════════════════════════════════════════════════════════════════════════
print("\n=== A1. 纯函数：哪些异常算「网络抖动」（可重试）===")
_exc = _real_requests.exceptions
_transient = [
    ("SSLError(SSL EOF)", _exc.SSLError("SSL: UNEXPECTED_EOF_WHILE_READING")),
    ("ConnectionResetError", ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")),
    ("requests.ConnectionError", _exc.ConnectionError("boom")),
    ("requests.Timeout", _exc.Timeout("read timeout")),
    ("ChunkedEncodingError", _exc.ChunkedEncodingError("broken chunk")),
    ("socket.timeout", __import__("socket").timeout("timed out")),
]
for label, err in _transient:
    check(f"瞬时：{label} → 重试", cc._is_transient_download_error(err) is True)

_status_exc = []
for code in (408, 425, 429, 500, 502, 503, 504):
    e = _exc.HTTPError(f"HTTP {code}")
    e.response = types.SimpleNamespace(status_code=code)
    _status_exc.append((code, e))
for code, err in _status_exc:
    check(f"瞬时：HTTP {code} → 重试", cc._is_transient_download_error(err) is True)

for code in (400, 401, 403, 404, 410):
    e = _exc.HTTPError(f"HTTP {code}")
    e.response = types.SimpleNamespace(status_code=code)
    check(f"非瞬时：HTTP {code}（业务拒绝）→ 不重试",
          cc._is_transient_download_error(e) is False)

check("非瞬时：磁盘写满（ENOSPC）→ 不重试",
      cc._is_transient_download_error(OSError(28, "No space left on device")) is False)
check("非瞬时：无权限（EACCES）→ 不重试",
      cc._is_transient_download_error(PermissionError(13, "Permission denied")) is False)
check("MD5 不一致 → 交给 MD5 重试分支（算可恢复）",
      cc._is_transient_download_error(cc._DownloadMD5Mismatch("x")) is True)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== A2. 真实 _download_file_inner：网络中断退避重试后成功 ===")


class _FakeResp:
    def __init__(self, chunks, headers=None, status=200, raise_status=False,
                 fail_after=None):
        self.headers = headers or {}
        self._chunks = chunks
        self.status_code = status
        self._raise_status = raise_status
        self._fail_after = fail_after

    def raise_for_status(self):
        if self._raise_status:
            err = _real_requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = types.SimpleNamespace(status_code=self.status_code)
            raise err

    def iter_content(self, chunk_size=65536):
        sent = 0
        for c in self._chunks:
            sent += len(c)
            yield c
            if self._fail_after is not None and sent >= self._fail_after:
                # 模拟"连接在传输中途被掐断"：正文没给完就抛（content-length 对不上）
                raise _exc.ChunkedEncodingError("连接在传输中途断了")


class _TimeStub:
    """把 time.sleep 换成"记账"，别让验收真的等 10 秒。"""
    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def __getattr__(self, name):
        return getattr(time, name)


class _HTTPStub:
    exceptions = _real_requests.exceptions

    def __init__(self):
        self.calls = []
        self.script = []

    def get(self, url, **kw):
        self.calls.append(url)
        item = self.script.pop(0) if self.script else _exc.ConnectionError("没有更多脚本")
        if isinstance(item, BaseException):
            raise item
        return item


def _make_client():
    return CloudClient(api_url="http://127.0.0.1:1", ws_url="", token="t",
                       client_id="netres-test", parent=None)


def _make_task(tid, name="断网测试.pdf"):
    task = CloudTask({"task_id": tid, "order_id": 7, "file_name": name,
                      "download_url": f"http://example.invalid/{tid}"})
    return task


def _install_http(scripts, sleeps_out=None):
    stub = _HTTPStub()
    stub.script = list(scripts)
    cc.http_requests = stub
    stub.time = _TimeStub()
    cc.time = stub.time
    return stub


_orig_http = cc.http_requests
_orig_time = cc.time
try:
    payload_ok = b"%PDF-1.4 hello-resilience " * 40
    ok_resp = lambda: _FakeResp([payload_ok], headers={
        "Content-Disposition": 'attachment; filename="x.pdf"',
        "content-length": str(len(payload_ok))})

    # ① 先断一次，再成功
    client = _make_client()
    client._download_semaphore = threading.BoundedSemaphore(4)
    task = _make_task(9101)
    client._pending_tasks[9101] = task
    stub = _install_http([ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"),
                          ok_resp()])
    client._download_file_inner(task)
    check("① 断一次后重试成功 → 任务 ready", task.status == "ready", task.status)
    check("① 真的发生了重试（HTTP 被调用 2 次）", len(stub.calls) == 2, str(len(stub.calls)))
    check("① 退避了 1s 才重试", stub.time.sleeps[:1] == [1.0], str(stub.time.sleeps))

    # ② 一直断 → 有上限地重试，最后仍按失败上报（不无限重试、不再"一次就死"）
    client2 = _make_client()
    client2._download_semaphore = threading.BoundedSemaphore(4)
    task2 = _make_task(9102)
    client2._pending_tasks[9102] = task2
    stub2 = _install_http([_exc.SSLError("SSL: UNEXPECTED_EOF_WHILE_READING")] * 4)
    client2._download_file_inner(task2)
    check("② 连续断网 → 明确失败（status=error，不会假装成功）",
          task2.status == "error", task2.status)
    check("② 最多尝试 _DOWNLOAD_NET_ATTEMPTS 次（4 次）",
          len(stub2.calls) == cc._DOWNLOAD_NET_ATTEMPTS, str(len(stub2.calls)))
    check("② 退避序列 1/3/6 秒（只等 3 次）",
          stub2.time.sleeps == [1.0, 3.0, 6.0], str(stub2.time.sleeps))

    # ③ 4xx 业务拒绝 → 立刻放弃，一次都不重试
    client3 = _make_client()
    client3._download_semaphore = threading.BoundedSemaphore(4)
    task3 = _make_task(9103)
    client3._pending_tasks[9103] = task3
    stub3 = _install_http([_FakeResp([], status=404, raise_status=True)])
    client3._download_file_inner(task3)
    check("③ 404 → 失败", task3.status == "error", task3.status)
    check("③ 404 不重试（只请求 1 次）", len(stub3.calls) == 1, str(len(stub3.calls)))
    check("③ 404 不等待", stub3.time.sleeps == [], str(stub3.time.sleeps))

    # ④ 传输中途断（ChunkedEncodingError）也要能重试
    client4 = _make_client()
    client4._download_semaphore = threading.BoundedSemaphore(4)
    task4 = _make_task(9104)
    client4._pending_tasks[9104] = task4
    bad = _FakeResp([payload_ok[:100]], headers={"content-length": str(len(payload_ok))},
                    fail_after=50)
    stub4 = _install_http([bad, ok_resp()])
    client4._download_file_inner(task4)
    check("④ 传到一半断 → 重试成功", task4.status == "ready", task4.status)
    check("④ 半截文件被清掉重下（内容完整）",
          task4.local_path and open(task4.local_path, "rb").read() == payload_ok)

    # ⑤ MD5 不一致仍按老口径重试一次（新代码不能把这个分支弄丢）
    import hashlib
    client5 = _make_client()
    client5._download_semaphore = threading.BoundedSemaphore(4)
    task5 = _make_task(9105)
    task5.source_md5 = hashlib.md5(payload_ok).hexdigest()
    client5._pending_tasks[9105] = task5
    wrong = _FakeResp([b"%PDF-1.4 WRONG " * 10])
    stub5 = _install_http([wrong, ok_resp()])
    client5._download_file_inner(task5)
    check("⑤ MD5 不一致 → 重下一遍后成功", task5.status == "ready", task5.status)
    check("⑤ MD5 不一致那条不占用网络退避次数（sleeps 为空）",
          stub5.time.sleeps == [], str(stub5.time.sleeps))
finally:
    cc.http_requests = _orig_http
    cc.time = _orig_time

# ══════════════════════════════════════════════════════════════════════════
print("\n=== B. 取号失败不静默 + 任何 -L 兜底都落本地离线库（GUI 集成，offscreen）===")
from PySide6.QtWidgets import QApplication   # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui                           # noqa: E402
import converter as _converter               # noqa: E402
from gui import MainWindow                   # noqa: E402

_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

GUI_SRC = io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
CC_SRC = io.open(os.path.join(LOCAL_TOOL, "cloud_client.py"), encoding="utf-8").read()
PP_SRC = io.open(os.path.join(LOCAL_TOOL, "pdf_printer.py"), encoding="utf-8").read()
MAIN_SRC = io.open(os.path.join(LOCAL_TOOL, "main.py"), encoding="utf-8").read()

win = MainWindow(config_path=paths.config_path())
_offline = win._offline_sync
check("前提：沙箱里离线库可用", _offline is not None)


def _rows():
    try:
        return _offline.count_unsynced()
    except Exception:
        return -1


_files = [{"file_name": "a.pdf", "page_count": 1, "copies": 1}]
_before = _rows()
_wrote = win._cache_local_order_locally(
    "HN20261104-L0006", _files, 0.7, "2026-11-04 12:37:54", None, reason="取号超时")
check("① -L 临时号被写进本地离线库", _wrote is True and _rows() == _before + 1,
      f"wrote={_wrote} rows={_rows()}")
check("① 库里的号就是那个 -L 号",
      _offline.find_pending_by_number("HN20261104-L0006") is not None)
_again = win._cache_local_order_locally(
    "HN20261104-L0006", _files, 0.7, "2026-11-04 12:37:54", None, reason="取号超时")
check("② 同一个号再存一次是幂等的（不重复插行）",
      _again is False and _rows() == _before + 1, f"again={_again} rows={_rows()}")
_wrote_cloud = win._cache_local_order_locally(
    "HN20261104-0023", _files, 0.7, "2026-11-04 12:37:54", None)
check("③ 已经是云端号（不带 -L）→ 不进离线库（避免重复上报）",
      _wrote_cloud is False and _rows() == _before + 1)
check("③ 云端号确实没被写进库",
      _offline.find_pending_by_number("HN20261104-0023") is None)

print("\n=== B2. 源码口径：三条兜底路径都必须落库，取号失败必须留证 ===")
check("超时兜底（>4s）分支把 -L 号落库",
      'reason="取号超时"' in GUI_SRC)
check("net 分支回 -L（取号失败）后也落库",
      'reason="取号失败"' in GUI_SRC)
check("落库发生在返回订单号之前（否则调用方已经开打了）",
      GUI_SRC.index('reason="取号失败"') < GUI_SRC.index(
          'return result.get("order_number") or "", bool(result.get("uploaded", False))'))
check("取号失败有 HTTP 状态码日志（原来是静默）",
      'f"取号失败：HTTP {resp.status_code}' in GUI_SRC)
check("取号被业务拒绝（200 但 success=false）也有日志",
      '取号被云端拒绝（HTTP {resp.status_code}）' in GUI_SRC)
check("窗口提示里也告诉操作员「将用本地临时号先打」",
      "将用本地临时号先打" in GUI_SRC)
check("落库函数只认 -L / LOCAL- 前缀（别的号绝不进离线库）",
      'if "-L" not in _up and not _up.startswith("LOCAL-"):' in GUI_SRC)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== C. 崩溃取证：faulthandler + 线程 excepthook ===")
import faulthandler                             # noqa: E402
import main as _main                            # noqa: E402

_crash_path = _main.install_crash_handlers(None)
check("install_crash_handlers 返回日志路径", bool(_crash_path), _crash_path)
check("崩溃日志文件已创建", os.path.isfile(_crash_path))
with io.open(_crash_path, encoding="utf-8", errors="replace") as f:
    _head = f.read()
check("文件里记了进程启动那行（能对上崩溃时刻）",
      "进程启动" in _head and "pid=" in _head)
check("faulthandler 处于开启状态", faulthandler.is_enabled() is True)
check("threading.excepthook 已被接管（不再是默认实现）",
      threading.excepthook is not threading.__excepthook__)

# 变异自证：一个后台线程抛异常 → 必须写进同一份文件
_marker = f"netres-{int(time.time() * 1000)}"


def _boom():
    raise RuntimeError(_marker)


_t = threading.Thread(target=_boom, name="验收线程")
_t.start()
_t.join()
with io.open(_crash_path, encoding="utf-8", errors="replace") as f:
    _body = f.read()
check("★ 后台线程未捕获异常落盘了（旧实现里它一声不响地蒸发）",
      _marker in _body and "验收线程" in _body)
check("落盘内容带完整 traceback",
      "RuntimeError" in _body and "Traceback" in _body)

print("\n=== C2. 源码口径：接线位置 + GDI 面包屑 ===")
check("main() 里装了崩溃取证",
      "install_crash_handlers(logger)" in MAIN_SRC)
check("装在任何工作线程起来之前（早于 QApplication 创建）",
      MAIN_SRC.index("install_crash_handlers(logger)")
      < MAIN_SRC.index("app = QApplication(sys.argv)"))
check("GDI 打印：DC 创建后有 logger 面包屑（原来是 print()，打包版直接进黑洞）",
      "GDI: 打印机 DC 已创建" in PP_SRC)
check("GDI 打印：StartDoc 有面包屑", "GDI: StartDoc 已提交" in PP_SRC)
check("GDI 打印：每一面送出都有面包屑", "面已送出（PDF p." in PP_SRC)
check("GDI 打印：EndDoc 有面包屑", "GDI: EndDoc 完成" in PP_SRC)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== D. 收尾 ===")
try:
    win.close()
except Exception:
    pass
try:
    _offline.clear_all()
except Exception:
    pass

print("\n" + "=" * 60)
if ok_all:
    print("网络抖动健壮性验收: 全部通过 ✅")
    sys.exit(0)
print("网络抖动健壮性验收: 存在失败项 ❌")
sys.exit(1)
