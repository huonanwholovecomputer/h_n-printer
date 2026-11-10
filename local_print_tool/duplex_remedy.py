"""duplex_remedy.py — 手动双面「半成品补救」台账（纯逻辑）。

背景（2026-11-10 用户现场要求）
------------------------------
无自动双面器的机型（HP LaserJet 1020 等）打双面必须由程序分两趟 + 人工翻纸。
翻纸窗上那个「⏭ 跳过本任务」以及倒计时归零/关窗，会把任务**当场判失败**：

  · 第 1 趟的纸已经实打实出来了（半成品），程序却只留一条"请人工处理"的文字，
    然后把标签页冻结、把订单报成失败/放弃；
  · 操作员过一会儿把纸翻好回来了，程序**没有任何入口**能接着打第 2 趟
    （标签页已冻结，「开始打印」是灰的；就算能点也会重打一整份，白费纸）；
  · 于是现场就是"只打了前一半"或"只打了后一半"，纸废了、订单还得人工兜。

本模块只做**台账**：记下"哪一批纸是半成品、当时的两趟页序是什么、合并后的 PDF 在哪、
属于哪些任务/订单"。不碰 Windows / 打印机 / Qt，因此可以在任何机器上逐组合验收
（其他/tests/verify_duplex_remedy.py）。真正的补打在 gui.DuplexRemedyWorker。

三条设计口径（改这里前先读）
----------------------------
1. **页序不在本模块重算**：`build_plan()` 只把记录里的 `file_plans` 还原成
   `manual_duplex.FilePlan`，再调用 `manual_duplex.plan_passes()` —— 与第一次打印时
   完全同一条代码路径。任何"另算一套第 2 趟页序"的实现都会在补打时打错页。
2. **半成品 ≠ 失败**：跳过/超时/第 2 趟失败都只写 `state=pending` 的记录，
   任务上打 `remedy_pending` 标记；绝不在这里判失败、也不删合并 PDF。
   判失败只有两条路：操作员在补救窗里选「作废」，或补打真的成功。
3. **失败要能兜住**：清单损坏、记录缺字段、PDF 丢了，都只影响"这一条还能不能补打"，
   绝不能让 `load()` 抛异常 —— 那会让主界面启动就崩，比半成品本身更糟。

记录字段（JSON，全部可选/可缺，读时归一）
----------------------------------------
  id            记录号（时间 + pid，唯一）
  created_at / updated_at
  state         pending / resolved / abandoned
  stage         pass1_done（第 1 趟已打、第 2 趟未打）| pass2_partial（第 2 趟打了一半失败）
  reason        人话原因（操作员跳过 / 翻纸等待超时 / 第 2 趟失败: …）
  printer / paper / dpi        补打要用同一台打印机、同一种纸、同一 DPI
  rotate_mode   per_file（生产路径，逐文件按装订方向）| fixed（整批统一 rotate）
  rotate        rotate_mode=fixed 时的固定值
  first / reverse_second / second_rotate_180 / landscape_fill   档案参数（页序/补偿唯一来源）
  tray_foreign  第一次打印时出纸槽里压在本批下面的张数（文案用；补打时已不可信，只做提示）
  sheet_count / total_sides
  file_plans    [{label, pages, duplex, second_rotate}]
  items         各任务快照（含 tab_key / task_id / order_id / order_number / …），
                补打成功后用它把 job 找回来收尾，找不到就按云端接口直接回报
  pdf           合并后 PDF 的绝对路径（在 store_root() 下）
  attempts      已尝试补救的次数（每次"又打出半成品" +1）
  last_error    最近一次补救失败的原因
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from typing import Any

import manual_duplex
import paths as _paths

VERSION = 1
MANIFEST_NAME = "duplex_pending.json"
PDF_DIR_NAME = "duplex_pending_pdf"

# 已完成（resolved/abandoned）的记录保留天数：够查"上周那批是怎么收的"就行。
KEEP_DONE_DAYS = 30

STATE_PENDING = "pending"
STATE_RESOLVED = "resolved"
STATE_ABANDONED = "abandoned"
STATES = (STATE_PENDING, STATE_RESOLVED, STATE_ABANDONED)

STAGE_PASS1_DONE = "pass1_done"
STAGE_PASS2_PARTIAL = "pass2_partial"
STAGES = (STAGE_PASS1_DONE, STAGE_PASS2_PARTIAL)

ROTATE_PER_FILE = "per_file"
ROTATE_FIXED = "fixed"

# 半个批次最多记这么多任务/文件计划；超出说明记录被人手改坏了，宁可按"不可补打"处理。
MAX_ITEMS = 200
MAX_FILE_PLANS = 400

_lock = threading.RLock()


# ─────────────────────── 路径与基础工具 ───────────────────────

def manifest_path() -> str:
    """清单文件：%APPDATA%\\HN打印工具\\duplex_pending.json。

    与 print_config.json 平级（同一个用户数据根），但它**独立成文件**：
    标签页配置整体落盘/回滚时绝不能顺手把待补救的半成品记录抹掉。
    """
    return os.path.join(_paths.get_app_data_dir(), MANIFEST_NAME)


def store_root() -> str:
    """半成品 PDF 目录（%APPDATA%\\HN打印工具\\duplex_pending_pdf）。"""
    d = os.path.join(_paths.get_app_data_dir(), PDF_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_str(value, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def make_id(existing=()) -> str:
    """生成记录号：`20261110-132045-1234`（本地时间 + pid 尾号），重复就加序号。"""
    base = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 10000:04d}"
    taken = set(existing or ())
    if base not in taken:
        return base
    for n in range(1, 1000):
        cand = f"{base}-{n}"
        if cand not in taken:
            return cand
    return f"{base}-{int(time.time() * 1000) % 1000}"


# ─────────────────────── 归一（读时兜底） ───────────────────────

def _normalize_file_plan(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pages = []
    for p in (raw.get("pages") or []):
        n = _as_int(p, 0)
        if n > 0:
            pages.append(n)
    if not pages:
        return None
    rot = raw.get("second_rotate")
    return {
        "label": _as_str(raw.get("label")) or "文件",
        "pages": pages[:5000],
        "duplex": _as_bool(raw.get("duplex"), True),
        # None 有意义（= 跟档案默认走），别把 None 折成 False
        "second_rotate": (None if rot is None else _as_bool(rot)),
    }


def _normalize_item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "idx": _as_int(raw.get("idx"), -1),
        "tab_key": _as_str(raw.get("tab_key")),
        "task_id": _as_int(raw.get("task_id"), 0),
        "order_id": _as_int(raw.get("order_id"), 0),
        "order_number": _as_str(raw.get("order_number")),
        "display_name": _as_str(raw.get("display_name")),
        "file_path": _as_str(raw.get("file_path")),
        "copies": max(1, _as_int(raw.get("copies"), 1)),
        "duplex": _as_str(raw.get("duplex"), "off") or "off",
        "page_range": _as_str(raw.get("page_range")),
        "page_count": _as_int(raw.get("page_count"), 0),
        "owner_name": _as_str(raw.get("owner_name")),
        "is_admin_print": _as_bool(raw.get("is_admin_print"), True),
    }


def _normalize(raw) -> dict | None:
    """把读进来的一条记录修成可用形状；完全不可用的返回 None（会被静默丢弃）。"""
    if not isinstance(raw, dict):
        return None
    rid = _as_str(raw.get("id"))
    if not rid:
        return None
    state = _as_str(raw.get("state"), STATE_PENDING)
    if state not in STATES:
        state = STATE_PENDING
    stage = _as_str(raw.get("stage"), STAGE_PASS1_DONE)
    if stage not in STAGES:
        stage = STAGE_PASS1_DONE
    plans = []
    for fp in (raw.get("file_plans") or []):
        n = _normalize_file_plan(fp)
        if n is not None:
            plans.append(n)
        if len(plans) >= MAX_FILE_PLANS:
            break
    items = []
    for it in (raw.get("items") or []):
        n = _normalize_item(it)
        if n is not None:
            items.append(n)
        if len(items) >= MAX_ITEMS:
            break
    rec = {
        "version": VERSION,
        "id": rid,
        "created_at": _as_str(raw.get("created_at")) or _now(),
        "updated_at": _as_str(raw.get("updated_at")) or _as_str(raw.get("created_at")),
        "state": state,
        "stage": stage,
        "reason": _as_str(raw.get("reason")),
        "printer": _as_str(raw.get("printer")),
        "paper": _as_str(raw.get("paper")),
        "dpi": _as_int(raw.get("dpi"), 0),
        "rotate_mode": (_as_str(raw.get("rotate_mode"), ROTATE_PER_FILE)
                        if _as_str(raw.get("rotate_mode")) in (ROTATE_PER_FILE, ROTATE_FIXED)
                        else ROTATE_PER_FILE),
        "rotate": _as_bool(raw.get("rotate"), False),
        "first": (_as_str(raw.get("first"), manual_duplex.FIRST_ODD)
                  if _as_str(raw.get("first")) in (manual_duplex.FIRST_ODD,
                                                   manual_duplex.FIRST_EVEN)
                  else manual_duplex.FIRST_ODD),
        "reverse_second": _as_bool(raw.get("reverse_second"), True),
        "second_rotate_180": _as_bool(raw.get("second_rotate_180"), True),
        "landscape_fill": _as_bool(raw.get("landscape_fill"), True),
        "tray_foreign": max(0, _as_int(raw.get("tray_foreign"), 0)),
        "sheet_count": max(0, _as_int(raw.get("sheet_count"), 0)),
        "total_sides": max(0, _as_int(raw.get("total_sides"), 0)),
        "file_plans": plans,
        "items": items,
        "pdf": _as_str(raw.get("pdf")),
        "attempts": max(0, _as_int(raw.get("attempts"), 0)),
        "last_error": _as_str(raw.get("last_error")),
    }
    return rec


# ─────────────────────── 读写（原子 + 失败自愈） ───────────────────────

def load(include_done: bool = False) -> list[dict]:
    """读清单。任何异常都返回 []（绝不抛给调用方）；坏文件会被改名留证。

    include_done=False（默认）只返回 state=pending 的记录 —— 界面/判据都只关心这个。
    """
    with _lock:
        path = manifest_path()
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # 坏清单：改名留证（别覆盖，用户可能想手工救），本次按"没有待补救"处理。
            try:
                os.replace(path, path + time.strftime(".bad-%Y%m%d%H%M%S"))
            except OSError:
                pass
            return []
        raw_records = data.get("records") if isinstance(data, dict) else data
        if not isinstance(raw_records, list):
            return []
        out: list[dict] = []
        for raw in raw_records:
            rec = _normalize(raw)
            if rec is None:
                continue
            if not include_done and rec["state"] != STATE_PENDING:
                continue
            out.append(rec)
        return out


def load_all() -> list[dict]:
    """含已完成/已作废的全部记录（「补救记录」展示与清理用）。"""
    return load(include_done=True)


def save(records: list[dict]) -> bool:
    """原子写清单（tmp + os.replace）。返回 False = 写失败（调用方应至少记日志）。"""
    with _lock:
        path = manifest_path()
        payload = {"version": VERSION,
                   "saved_at": _now(),
                   "records": [r for r in (records or []) if isinstance(r, dict)]}
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
            return True
        except Exception:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False


def add(record: dict) -> dict:
    """追加一条新记录（会补 id / created_at / state），返回落盘后的记录。"""
    with _lock:
        recs = load(include_done=True)
        rid = _as_str(record.get("id")) or make_id([r.get("id") for r in recs])
        # id 必须**先**填进待归一的数据：_normalize() 把"没有 id 的记录"判为损坏并返回
        # None（那是读盘时的兜底）。make_record() 造出来的记录 id 正是空的，直接交给
        # _normalize 会整条被丢掉 —— 结果台账里只剩 id/time/state 四个字段，
        # 页序计划和半成品 PDF 全没了，等于"记了一条永远补不了的第 1 趟"。
        _raw = dict(record)
        _raw["id"] = rid
        rec = _normalize(_raw) or {}
        rec["id"] = rid
        rec["created_at"] = _as_str(record.get("created_at")) or _now()
        rec["updated_at"] = _now()
        rec["state"] = STATE_PENDING
        recs = [r for r in recs if r.get("id") != rid]
        recs.append(rec)
        save(recs)
        return rec


def get(rid: str, include_done: bool = True) -> dict | None:
    for rec in load(include_done=include_done):
        if rec.get("id") == rid:
            return rec
    return None


def update(rid: str, **fields) -> dict | None:
    """按 id 改字段（不会跨记录串写）。返回改后的记录；找不到返回 None。"""
    with _lock:
        recs = load(include_done=True)
        found = None
        for i, rec in enumerate(recs):
            if rec.get("id") != rid:
                continue
            merged = dict(rec)
            merged.update(fields)
            merged["id"] = rid
            merged["updated_at"] = _now()
            fixed = _normalize(merged)
            if fixed is None:
                return None
            recs[i] = fixed
            found = fixed
            break
        if found is None:
            return None
        save(recs)
        return found


def mark_stalled(rid: str, reason: str = "", stage: str = "") -> dict | None:
    """把一条记录标成"又打出半成品"：state=pending、attempts+1、reason 覆盖。"""
    rec = get(rid)
    if rec is None:
        return None
    fields = {"state": STATE_PENDING,
              "attempts": int(rec.get("attempts", 0)) + 1}
    if reason:
        fields["reason"] = reason
    if stage in STAGES:
        fields["stage"] = stage
    return update(rid, **fields)


def resolve(rid: str, note: str = "") -> None:
    """补打成功：记录转 resolved 并**删掉**半成品 PDF（纸已经是成品了）。"""
    _close(rid, STATE_RESOLVED, note)


def abandon(rid: str, note: str = "") -> None:
    """操作员选「作废」：记录转 abandoned，PDF 删除（那份纸按失败收尾）。"""
    _close(rid, STATE_ABANDONED, note)


def _close(rid: str, state: str, note: str) -> None:
    rec = get(rid)
    if rec is None:
        return
    update(rid, state=state, last_error=note or rec.get("last_error", ""))
    _delete_pdf(rec.get("pdf"))


def remove(rid: str, delete_pdf: bool = True) -> None:
    """从清单里彻底删掉一条记录（测试/人工清理用）。"""
    with _lock:
        recs = load(include_done=True)
        keep = []
        target = None
        for rec in recs:
            if rec.get("id") == rid:
                target = rec
            else:
                keep.append(rec)
        save(keep)
    if delete_pdf and target is not None:
        _delete_pdf(target.get("pdf"))


def prune(days: int = KEEP_DONE_DAYS) -> int:
    """清理超过 days 天的已完成/已作废记录（连同 PDF）。返回清理条数。"""
    cutoff = time.time() - max(1, int(days)) * 86400
    with _lock:
        recs = load(include_done=True)
        keep, drop = [], []
        for rec in recs:
            if rec.get("state") == STATE_PENDING:
                keep.append(rec)
                continue
            try:
                ts = time.mktime(time.strptime(rec.get("created_at", ""), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                ts = 0.0
            (drop if (ts and ts < cutoff) else keep).append(rec)
        if drop:
            save(keep)
            for rec in drop:
                _delete_pdf(rec.get("pdf"))
        return len(drop)


def _delete_pdf(path: str) -> None:
    p = _as_str(path)
    if not p:
        return
    try:
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass


def pending() -> list[dict]:
    """按创建时间倒序的待补救记录（最新的在最上面，与界面列表同序）。"""
    recs = load(include_done=False)
    recs.sort(key=lambda r: _as_str(r.get("created_at")), reverse=True)
    return recs


def pending_count() -> int:
    return len(load(include_done=False))


# ─────────────────────── 半成品 PDF 落盘 ───────────────────────

def keep_pdf(src: str, rid: str) -> str:
    """把合并后的半成品 PDF 复制进台账目录，返回新路径（失败返回 ""）。

    为什么复制而不是原地保留：原始 merged 在 %TEMP%，系统清理/重启就会没了；
    台账目录在同一用户数据根下，重启后仍能补打。
    """
    if not src or not os.path.isfile(src):
        return ""
    try:
        os.makedirs(store_root(), exist_ok=True)
        dst = os.path.join(store_root(), f"{rid}.pdf")
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return ""


def pdf_available(rec: dict) -> bool:
    return bool(_as_str(rec.get("pdf"))) and os.path.isfile(_as_str(rec.get("pdf")))


def problems(rec: dict) -> list[str]:
    """这条记录还能不能补打；返回人话问题清单（空 = 可以）。"""
    out: list[str] = []
    if not isinstance(rec, dict):
        return ["记录损坏"]
    if not pdf_available(rec):
        out.append("合并后的 PDF 已不存在（可能被清理或误删）")
    if not rec.get("file_plans"):
        out.append("记录里没有页序计划（无法重建第 2 趟）")
    try:
        build_plan(rec)
    except Exception as e:
        out.append(f"页序计划无法重建: {type(e).__name__}: {e}")
    return out


# ─────────────────────── 页序重建（唯一来源仍是 manual_duplex） ───────────────────────

def build_plan(rec: dict) -> manual_duplex.PrintPlan:
    """由记录重建两趟计划。第一次打印与补打走的是**同一条** plan_passes。"""
    plans = []
    for fp in (rec.get("file_plans") or []):
        pages = [int(p) for p in (fp.get("pages") or []) if int(p) > 0]
        if not pages:
            continue
        rot = fp.get("second_rotate")
        if rec.get("rotate_mode") == ROTATE_FIXED:
            rot = bool(rec.get("rotate"))
        plans.append(manual_duplex.FilePlan(
            _as_str(fp.get("label")) or "文件", pages,
            bool(fp.get("duplex", True)),
            second_rotate=(None if rot is None else bool(rot))))
    return manual_duplex.plan_passes(
        plans,
        copies=1,
        first=_as_str(rec.get("first")) or manual_duplex.FIRST_ODD,
        reverse_second=_as_bool(rec.get("reverse_second"), True),
        second_rotate_180=_as_bool(rec.get("second_rotate_180"), True),
    )


def file_plans_to_dicts(file_plans) -> list[dict]:
    """把 manual_duplex.FilePlan 列表转成可 JSON 的 dict（写台账用）。"""
    out = []
    for fp in (file_plans or []):
        out.append({
            "label": _as_str(getattr(fp, "label", "")) or "文件",
            "pages": [int(p) for p in (getattr(fp, "pages", []) or [])],
            "duplex": bool(getattr(fp, "duplex", True)),
            "second_rotate": getattr(fp, "second_rotate", None),
        })
    return out


def make_record(*, stage: str, reason: str, printer: str, paper: str = "", dpi: int = 0,
                rotate_mode: str = ROTATE_PER_FILE, rotate: bool = False,
                first: str = manual_duplex.FIRST_ODD, reverse_second: bool = True,
                second_rotate_180: bool = True, landscape_fill: bool = True,
                tray_foreign: int = 0, sheet_count: int = 0, total_sides: int = 0,
                file_plans=(), items=(), pdf: str = "") -> dict:
    """构造一条记录（不落盘；落盘用 add()）。

    items 里每项应带 tab_key，补打成功后主界面据此把 job 找回来收尾。
    """
    return {
        "version": VERSION,
        "id": "",
        "created_at": _now(),
        "updated_at": _now(),
        "state": STATE_PENDING,
        "stage": stage if stage in STAGES else STAGE_PASS1_DONE,
        "reason": _as_str(reason),
        "printer": _as_str(printer),
        "paper": _as_str(paper),
        "dpi": _as_int(dpi, 0),
        "rotate_mode": (rotate_mode if rotate_mode in (ROTATE_PER_FILE, ROTATE_FIXED)
                        else ROTATE_PER_FILE),
        "rotate": bool(rotate),
        "first": first if first in (manual_duplex.FIRST_ODD, manual_duplex.FIRST_EVEN)
                 else manual_duplex.FIRST_ODD,
        "reverse_second": bool(reverse_second),
        "second_rotate_180": bool(second_rotate_180),
        "landscape_fill": bool(landscape_fill),
        "tray_foreign": max(0, _as_int(tray_foreign, 0)),
        "sheet_count": max(0, _as_int(sheet_count, 0)),
        "total_sides": max(0, _as_int(total_sides, 0)),
        "file_plans": file_plans_to_dicts(file_plans),
        "items": [dict(it) for it in (items or []) if isinstance(it, dict)],
        "pdf": _as_str(pdf),
        "attempts": 0,
        "last_error": "",
    }


# ─────────────────────── 文案 ───────────────────────

def _fmt_pages(pages) -> str:
    """页码序列 → "1 3 5" / "空白 2 4"。"""
    return " ".join("空白" if p is None else str(p) for p in (pages or []))


def stage_label(rec: dict) -> str:
    if _as_str(rec.get("stage")) == STAGE_PASS2_PARTIAL:
        return "第 2 趟打了一半失败"
    return "第 1 趟已打 · 第 2 趟未打"


def order_text(rec: dict) -> str:
    nums = []
    for it in (rec.get("items") or []):
        n = _as_str(it.get("order_number"))
        if n and n not in nums:
            nums.append(n)
    return "、".join(nums)


def files_text(rec: dict, limit: int = 4) -> str:
    names = []
    for it in (rec.get("items") or []):
        n = _as_str(it.get("display_name")) or os.path.basename(_as_str(it.get("file_path")))
        if n and n not in names:
            names.append(n)
    if len(names) > limit:
        return "、".join(names[:limit]) + f" 等 {len(names)} 个文件"
    return "、".join(names)


def summary_line(rec: dict) -> str:
    """列表里的一行（不带换行）。"""
    order = order_text(rec)
    return (f"{_as_str(rec.get('created_at'))} · {stage_label(rec)} · "
            f"{_as_int(rec.get('sheet_count'), 0)} 张纸"
            + (f" · {order}" if order else ""))


def describe(rec: dict) -> str:
    """给操作员看的完整说明（含页序与处理步骤）。"""
    try:
        plan = build_plan(rec)
        p1 = _fmt_pages(plan.pass_pages(1))
        p2 = _fmt_pages(plan.pass_pages(2))
        sheets = plan.sheet_count
    except Exception as e:
        p1 = p2 = f"（页序重建失败：{type(e).__name__}: {e}）"
        sheets = _as_int(rec.get("sheet_count"), 0)
    stage = _as_str(rec.get("stage"))
    foreign = _as_int(rec.get("tray_foreign"), 0)
    lines = [
        f"【{stage_label(rec)}】{_as_str(rec.get('created_at'))}",
        f"打印机：{_as_str(rec.get('printer')) or '(未知)'}"
        f"    纸张：{_as_str(rec.get('paper')) or '打印机默认'}",
        f"本批 {sheets} 张纸"
        + (f"（出纸槽里当时还压着 {foreign} 张别的纸，不在本批里）" if foreign > 0 else ""),
    ]
    order = order_text(rec)
    if order:
        lines.append(f"订单：{order}")
    files = files_text(rec)
    if files:
        lines.append(f"文件：{files}")
    lines.append(f"第 1 趟已打页：{p1}")
    lines.append(f"第 2 趟应打页：{p2}")
    if _as_str(rec.get("reason")):
        lines.append(f"原因：{_as_str(rec.get('reason'))}")
    if _as_int(rec.get("attempts"), 0) > 0:
        lines.append(f"已尝试补救 {_as_int(rec.get('attempts'), 0)} 次"
                     + (f"（最近一次：{rec.get('last_error')}）"
                        if _as_str(rec.get("last_error")) else ""))
    probs = problems(rec)
    if probs:
        lines.append("⚠ 这条记录不能直接补打：" + "；".join(probs))
    lines.append("")
    if stage == STAGE_PASS2_PARTIAL:
        lines.append("处理办法：第 2 趟已经打了一部分，纸叠不能再用 —— "
                     "请取出废纸、放好白纸后点「🖨 整单重打」（两趟重来）；"
                     "或点「✖ 作废」（按失败收尾）。")
    else:
        lines.append("处理办法（补打第 2 趟）：")
        lines.append(f"  1. 从出纸槽取出这批 {sheets} 张纸"
                     + (f"（只取最上面 {sheets} 张，下面那 {foreign} 张别动）"
                        if foreign > 0 else "")
                     + "，不改变方向、不翻面、不重排。")
        lines.append("  2. 保持相同方向插入纸盘 2，让已打印面朝下。")
        lines.append("  3. 点「▶ 补打第 2 趟」——程序只打第 2 趟，不会再打第 1 趟。")
        lines.append("  ⚠ 之后若又在出纸槽里打了别的纸，先单独取出本批；"
                     "分不清就用「🖨 整单重打」（会重新打两趟，需要白纸）。")
    return "\n".join(lines)
