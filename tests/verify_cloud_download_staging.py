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

print("=== 1b. 文件名解析：中文名不能被响应头的 ASCII 兜底吃掉（2026-10 线上事故）===")
# 线上实测的响应头长这样（Flask/Werkzeug 对非 ASCII 名做 encode("ascii","ignore") 兜底）：
#   attachment; filename=.pdf; filename*=UTF-8''%E4%B8%89....pdf
# 主名被吃光、只剩扩展名；老写法只读第一个 filename= → 落盘成 ".pdf" → os.path.splitext 认为
# "没有扩展名" → 类型判定为空 → 打印失败（#1004~#1007 就是这么炸的）。
WERK_CD = ("attachment; filename=.pdf; "
           "filename*=UTF-8''%E4%B8%89%E9%A1%B5%E6%96%87%E6%A1%A3%E6%B5%8B%E8%AF%95.pdf")
check("★ 任务自带中文名 → 用任务名（响应头的 ASCII 兜底不参与）",
      cc.resolve_download_name("三页文档测试.pdf", WERK_CD, 1006) == "三页文档测试.pdf",
      cc.resolve_download_name("三页文档测试.pdf", WERK_CD, 1006))
check("★ 任务名缺失时才用响应头，且优先 filename*（percent-decoded UTF-8）",
      cc.resolve_download_name("", WERK_CD, 1006) == "三页文档测试.pdf",
      cc.resolve_download_name("", WERK_CD, 1006))
check("响应头只有引号字符串也能解析",
      cc.resolve_download_name("", 'attachment; filename="云端订单.pdf"', 7) == "云端订单.pdf")
check("★ 只剩 \".pdf\" 时补安全主名（点开头名会让 splitext 认为没扩展名）",
      cc.resolve_download_name("", "attachment; filename=.pdf", 9) == "cloud_task_9.pdf"
      and cc.resolve_download_name("", "", 9) == "cloud_task_9.dat")
check("路径穿越/非法字符被压平（安全）",
      cc.resolve_download_name("..\\..\\evil.pdf", "", 3) == "evil.pdf"
      and cc.resolve_download_name("a<b>c|d.pdf", "", 4) == "a_b_c_d.pdf")
check("ASCII 名不受影响", cc.resolve_download_name("tmp_abc.pdf", WERK_CD, 5) == "tmp_abc.pdf")

# 端到端：同样的响应头走完整下载流程，落盘名必须是中文原名
task1b = CloudTask({"task_id": 4248, "order_id": 7, "file_name": "三页文档测试.pdf",
                    "download_url": "http://example.invalid/f1b"})
client._pending_tasks[4248] = task1b
cc.http_requests.get = lambda url, **kw: _FakeResp(
    [b"%PDF-1.4 cn " * 40], headers={"Content-Disposition": WERK_CD})
client._download_file_inner(task1b)
check("★ 端到端：落盘文件名仍是中文原名（不再变成 .pdf）",
      task1b.status == "ready" and os.path.basename(task1b.local_path) == "三页文档测试.pdf",
      f"{task1b.status} / {os.path.basename(task1b.local_path or '')}")
check("★ 下游类型判定认得它（converter.ext_of 不再返回空）",
      __import__("converter").ext_of(task1b.local_path) == ".pdf",
      __import__("converter").ext_of(task1b.local_path or ""))
check("★ 即使名字真成了 \".pdf\"，类型判定也能认出来（最后一环兜底）",
      __import__("converter").ext_of(os.path.join(_SANDBOX, ".pdf")) == ".pdf")
# 收尾：只清掉本节下载的那个文件（保留第 1 节的状态，后面几节的清扫计数才不受影响）
check("本节收尾只清自己的产物",
      staging.sweep({task.local_path}) == 1 and os.path.isfile(task.local_path))

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

print("=== 8. 缓存命中：复制成任务自持副本（共享缓存被清不再波及待打任务）===")
# 缺陷（D5 第 9 条）：缓存命中时旧写法把 task.local_path 直接指向
# `pdf_cache\<key>.pdf`（**跨任务共享**），而三条清理路径都会删它，GUI 的按引用清扫
# 只保护 `file_cache\cloud_<task_id>\` —— 任务还挂在列表上（预约单要等数小时）时文件
# 被清掉，点打印报「文件不存在」。
_cache_client = _make_client()
_cache_client._download_semaphore = __import__("threading").BoundedSemaphore(4)
_shared_src = os.path.join(_SANDBOX, "shared_src.pdf")
_shared_payload = b"%PDF-1.4 shared cache " * 40
with open(_shared_src, "wb") as f:
    f.write(_shared_payload)
_cache_client._save_pdf_to_cache("md5cache009", _shared_src, "共享缓存.pdf", ".pdf")
_shared_path = os.path.join(_cache_client._cache_dir, cc.pdf_cache_key("md5cache009") + ".pdf")
check("前提：共享缓存文件已就位", os.path.isfile(_shared_path), _shared_path)

task7 = CloudTask({"task_id": 4250, "order_id": 7, "file_name": "共享缓存.pdf",
                   "download_url": "http://example.invalid/f7", "source_md5": "md5cache009"})
_cache_client._pending_tasks[4250] = task7
cc.http_requests.get = lambda url, **kw: (_ for _ in ()).throw(RuntimeError("缓存命中不应发起网络请求"))
_cache_client._download_file_inner(task7)
check("★ 缓存命中后 local_path 是任务自持副本（不再是 pdf_cache 里的共享文件）",
      task7.status == "ready" and task7.local_path != _shared_path
      and staging.is_staged(task7.local_path), f"{task7.status} {task7.local_path}")
check("副本按 task_id 分目录（cloud_4250）",
      os.path.basename(os.path.dirname(task7.local_path)) == "cloud_4250",
      os.path.dirname(task7.local_path))
check("副本内容完整且保留原文件名",
      open(task7.local_path, "rb").read() == _shared_payload
      and os.path.basename(task7.local_path) == "共享缓存.pdf")
check("命中后不留「使用中」标记（已经挂到任务上，由引用保护）",
      staging._reserved_snapshot() == set(), str(staging._reserved_snapshot()))
# 判别式：同一次清扫里，无引用的副本必须被回收、被任务引用的副本必须留下。
_decoy_dir = os.path.join(paths.file_cache_dir(), "cloud_9999")
os.makedirs(_decoy_dir, exist_ok=True)
_decoy = os.path.join(_decoy_dir, "decoy.pdf")
with open(_decoy, "wb") as f:
    f.write(b"%PDF-1.4 decoy")
_removed = staging.sweep({task7.local_path})
check("副本被任务引用 → 按引用清扫保留它（同批无引用的副本被回收）",
      os.path.isfile(task7.local_path) and not os.path.isfile(_decoy) and _removed >= 1,
      f"removed={_removed} 引用副本还在={os.path.isfile(task7.local_path)} "
      f"无引用副本还在={os.path.isfile(_decoy)}")

# 关键回归：共享缓存真的被清理（保留期到期）后，待打任务的文件必须还在
_cache_client._CACHE_RETENTION_HOURS = 1
_idx = _cache_client._load_cache_index()
_idx[cc.pdf_cache_key("md5cache009")]["created_at"] = "2020-01-01 00:00:00"
_cache_client._save_cache_index(_idx)
_cache_client._cleanup_pdf_cache()
check("缓存清理确实删掉了共享缓存本体（制造出改前会丢文件的现场）",
      not os.path.isfile(_shared_path), _shared_path)
check("★ 共享缓存被清后任务文件仍可读（改前这里就是打印报「文件不存在」）",
      os.path.isfile(task7.local_path)
      and open(task7.local_path, "rb").read()[:5] == b"%PDF-")

print("=== 8b. 暂存目录不可用 → 回退共享缓存本体（功能不退化）===")
_shared2 = os.path.join(_SANDBOX, "shared2.pdf")
with open(_shared2, "wb") as f:
    f.write(b"%PDF-1.4 second " * 20)
_cache_client._save_pdf_to_cache("md5cache010", _shared2, "回退缓存.pdf", ".pdf")
_shared2_path = os.path.join(_cache_client._cache_dir, cc.pdf_cache_key("md5cache010") + ".pdf")
task8 = CloudTask({"task_id": 4251, "order_id": 7, "file_name": "回退缓存.pdf",
                   "download_url": "http://example.invalid/f8", "source_md5": "md5cache010"})
_cache_client._pending_tasks[4251] = task8
_orig_ensure = staging.ensure_dir_for
staging.ensure_dir_for = lambda p: False
try:
    _cache_client._download_file_inner(task8)
finally:
    staging.ensure_dir_for = _orig_ensure
check("暂存不可用 → 回退直指共享缓存（旧行为，不报错、仍可打印）",
      task8.status == "ready" and task8.local_path == _shared2_path,
      f"{task8.status} {task8.local_path}")
check("回退路径不留「使用中」标记", staging._reserved_snapshot() == set(),
      str(staging._reserved_snapshot()))

print("=== 9. 收尾 ===")
staging.sweep(set())
check("暂存目录无残留", staging.cache_size()[0] == 0, str(staging.cache_size()))

shutil.rmtree(_SANDBOX, ignore_errors=True)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
