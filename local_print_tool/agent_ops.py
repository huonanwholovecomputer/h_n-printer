"""
agent_ops.py — 智能助手（DSH）控制接口的 op 实现

分工
----
`agent_api.py` 只管传输/鉴权/线程排队；本模块管「op 名字 → 打印工具里真正该做的动作」。
所有方法都**只能在 GUI 主线程**被调用（由 `MainWindow._pump_agent_api` 的 QTimer 驱动）。

设计原则（都是本仓库踩过的坑）
------------------------------
1. **不另写一套数据/计费口径**：页数、张数、金额一律调 `printer_config` 里的同一个函数
   （`calc_cost` / `_count_pages_in_range` / `check_simplex_pages` / `calc_extra_total`），
   与界面上的数字必须逐分一致（硬规矩 7）。
2. **加文件只走现有入口** `win._add_files_to_table()`：暂存副本、重复跳过、格式校验、
   预转换线程、首加时间、字体检查全在那条路径上；接口里重写一遍必然漂移。
3. **写操作先过总闸门**：标签页冻结（已出过订单的快照）、正在打印、正在取号，
   一律拒绝而不是"尽力而为"——半个改动比不改更糟。
4. **失败要能被调用方程序化区分**：`AgentOpError(code, message)`，错误码稳定、消息给人看。
5. **每个写 op 写一条 GUI 日志**：操作员在日志区能看见"是助手干的"，而不是莫名其妙多出文件。

op 清单（params 都是 JSON 对象）
--------------------------------
    status                              → 程序/打印机/忙碌状态
    tabs.list                           → 全部标签页摘要
    tabs.get        {tab}               → 单个标签页明细（含每个文件）
    tabs.create     {switch?}           → 新建标签页，返回 tab
    tabs.update     {tab, ...选项}      → 改附加服务/归属/备注
    tabs.delete     {tab, force?}       → 删除标签页
    tabs.clear      {tab}               → 清空标签页里的文件（保留标签页）
    tabs.print      {tab, confirm:true} → 开始打印（需显式确认）
    files.add       {paths, tab?, new_tab?, options?, switch?}
    files.update    {tab, index, ...选项}
    files.remove    {tab, index}
    cloud.orders    {order?, status?, source?, owner?, file?, since?, until?, limit?, offset?}
                                     → 云端订单数据（只读、不限时间；走设备 token）
"""

from __future__ import annotations

import logging
import os
import threading
import time

import requests as http_requests

import printer_config as pc
import paths
from agent_api import AgentOpError

logger = logging.getLogger(__name__)

# 与 `MainWindow.__add_files_to_table_impl` 里的 allowed_types 必须一致
# （tests/verify_agent_api.py 会解析 gui.py 源码比对，防两处漂移）。
ALLOWED_EXTS = {
    ".pdf", ".doc", ".docx",
    ".xls", ".xlsx",
    ".txt", ".md",
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
HTML_EXTS = {".html", ".htm"}
ENGINES = {"word", "wps", "libreoffice"}
DPI_CHOICES = {0, 200, 300, 400, 600}
ORIENT_CHOICES = {"auto", "landscape", "portrait"}
MAX_FILES_PER_CALL = 50
DELIVERY_FIELDS = ("delivery_enabled", "delivery_location", "urgency", "cover_page",
                   "cover_page_price", "owner_name", "is_admin_print", "remark")
JOB_FIELDS = ("copies", "duplex", "duplex_mode", "page_range", "simplex_enabled",
              "simplex_pages", "display_name", "remark", "engine", "dpi", "image_orientation")


def _as_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AgentOpError("invalid", f"{name} 必须是整数（收到 {value!r}）")


def _as_str(value, name: str) -> str:
    if not isinstance(value, str):
        raise AgentOpError("invalid", f"{name} 必须是字符串（收到 {value!r}）")
    return value


def _norm_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


class AgentOps:
    """op 分发器。`win` = MainWindow（只用它已有的能力，不改它的行为）。"""

    def __init__(self, win, app_version: str = ""):
        self.win = win
        self.app_version = app_version

    # ---- 入口 ----

    def __call__(self, op: str, params: dict) -> dict:
        fn = _OPS.get(op)
        if fn is None:
            raise AgentOpError("unknown_op",
                               f"未知 op: {op}（可用: {', '.join(sorted(_OPS))}）")
        if not isinstance(params, dict):
            raise AgentOpError("invalid", "params 必须是 JSON 对象")
        return fn(self, params)

    # ---- 小工具 ----

    @property
    def cfg(self):
        return self.win._config

    def _tabs(self) -> dict:
        return self.win._config.tabs

    def _tab(self, key=None):
        """取标签页：key 空 = 当前页。返回 (key, tab)，不存在即报错。"""
        k = str(key).strip() if key not in (None, "") else str(self.win._current_tab)
        tab = self._tabs().get(k)
        if tab is None:
            raise AgentOpError("not_found", f"标签页 {k} 不存在（现有: "
                                            f"{', '.join(sorted(self._tabs()))}）")
        return k, tab

    def _require_writable(self, key: str, tab) -> None:
        """写操作总闸门：冻结 / 正在打印。"""
        if getattr(tab, "frozen", False):
            raise AgentOpError("frozen", f"标签页 {key} 已冻结（打印过的订单快照，不能改）")
        try:
            inflight = self.win._tab_jobs_in_flight(key)
        except Exception:
            inflight = []
        if inflight:
            raise AgentOpError("busy", f"标签页 {key} 的文件正在打印，不能改")

    def _is_current(self, key: str) -> bool:
        return str(self.win._current_tab) == str(key)

    def _save(self, key: str, jobs: list) -> None:
        if self._is_current(key):
            self.win._set_current_jobs(jobs)
        else:
            self.win._save_config()

    def _refresh_rows(self, key: str, jobs: list, rows: list) -> None:
        """应用改动后刷新界面（只刷当前页；后台页只落盘）。"""
        if not self._is_current(key):
            return
        for row in rows:
            if 0 <= row < len(jobs):
                try:
                    self.win._fill_table_row(row, jobs[row])
                except Exception as e:
                    logger.warning(f"[agent-api] 刷新第 {row} 行失败: {e}")
        self.win._update_total_cost()

    def _total(self, tab) -> tuple[float, bool, int]:
        """(合计金额, 是否有未知页数, 选中页数×份数)。口径与 `_update_total_cost` 完全一致。"""
        base = 0.0
        unknown = False
        pages = 0
        for job in tab.jobs:
            cost, _ = pc.calc_cost(job.page_count, job.copies, job.duplex,
                                   self.cfg.simplex_price, self.cfg.duplex_price,
                                   job.page_range, pc.job_simplex_pages(job))
            base += cost
            if int(getattr(job, "page_count", 0) or 0) <= 0:
                unknown = True
            pages += int(pc._count_pages_in_range(job.page_range or "",
                                                  job.page_count or 0)) * max(1, int(job.copies or 1))
        extra = tab.calc_extra_total(base, self.cfg) if tab else 0.0
        return round(base + extra, 2), unknown, pages

    def _brief(self, key: str, tab) -> dict:
        total, unknown, pages = self._total(tab)
        return {
            "tab": str(key),
            "files": len(tab.jobs),
            "pages": pages,
            "cost": total,
            "pages_unknown": unknown,
            "frozen": bool(getattr(tab, "frozen", False)),
            "printing": str(getattr(self.win, "_printing_tab_key", "") or "") == str(key),
            "owner_name": getattr(tab, "owner_name", ""),
            "is_admin_print": bool(getattr(tab, "is_admin_print", False)),
            "delivery_enabled": bool(getattr(tab, "delivery_enabled", False)),
            "delivery_location": getattr(tab, "delivery_location", ""),
            "urgency": getattr(tab, "urgency", "低"),
            "cover_page": bool(getattr(tab, "cover_page", False)),
            "remark": getattr(tab, "remark", ""),
        }

    def _job_brief(self, index: int, job) -> dict:
        total_pages = int(getattr(job, "page_count", 0) or 0)
        selected = int(pc._count_pages_in_range(job.page_range or "", total_pages))
        cost, _ = pc.calc_cost(job.page_count, job.copies, job.duplex,
                               self.cfg.simplex_price, self.cfg.duplex_price,
                               job.page_range, pc.job_simplex_pages(job))
        name = getattr(job, "display_name", "") or os.path.basename(job.file_path or "")
        return {
            "index": index,
            "name": name,
            "path": getattr(job, "source_path", "") or job.file_path,
            "copies": int(getattr(job, "copies", 1) or 1),
            "duplex": getattr(job, "duplex", "off"),
            "duplex_mode": getattr(job, "duplex_mode", ""),
            "page_range": getattr(job, "page_range", ""),
            "page_count": total_pages,
            "pages_selected": selected,
            "simplex_enabled": bool(getattr(job, "simplex_enabled", False)),
            "simplex_pages": getattr(job, "simplex_pages", ""),
            "remark": getattr(job, "remark", ""),
            "engine": getattr(job, "engine", ""),
            "dpi": int(getattr(job, "dpi", 0) or 0),
            "image_orientation": getattr(job, "image_orientation", "auto"),
            "cost": round(cost, 2),
            "order_id": getattr(job, "order_id", None),
        }

    # ══════════════════════════════════════════════════════════════════
    # status / tabs
    # ══════════════════════════════════════════════════════════════════

    def op_status(self, params: dict) -> dict:
        win = self.win
        try:
            busy = bool(win._stack_busy())
        except Exception:
            busy = False
        printing = str(getattr(win, "_printing_tab_key", "") or "") if busy else ""
        api = getattr(win, "_agent_api", None)
        try:
            pending = int(api.pending()) if api is not None else 0
        except Exception:
            pending = 0
        notes = []
        if getattr(win, "_print_starting", False):
            notes.append("正在取号/上报订单")
        if getattr(win, "_closing", False):
            notes.append("程序正在退出")
        return {
            "app": "h_n-printer",
            "version": self.app_version,
            "pid": os.getpid(),
            "config_path": paths.config_path(),
            "tab_count": len(self._tabs()),
            "current_tab": str(win._current_tab),
            "printing_tab": printing,
            "busy": busy or bool(getattr(win, "_print_starting", False)),
            "stack_mode": bool(getattr(self.cfg, "duplex_stack_mode", False)),
            "printer_name": getattr(self.cfg, "printer_name", ""),
            "members": len(win._member_names()),
            "cloud_enabled": bool(getattr(self.cfg, "cloud_enabled", False)),
            "cloud_connected": bool(getattr(win, "_cloud_connected", False)),
            "pending_calls": pending,
            "notes": notes,
        }

    def op_tabs_list(self, params: dict) -> dict:
        tabs = self._tabs()
        keys = self.win._sorted_tab_keys(tabs)
        return {"current_tab": str(self.win._current_tab),
                "tabs": [self._brief(k, tabs[k]) for k in keys]}

    def op_tabs_get(self, params: dict) -> dict:
        key, tab = self._tab(params.get("tab"))
        data = self._brief(key, tab)
        data["jobs"] = [self._job_brief(i, j) for i, j in enumerate(tab.jobs)]
        return data

    def op_tabs_create(self, params: dict) -> dict:
        prev = str(self.win._current_tab)
        key = self.win._new_tab()
        if not params.get("switch", True) and prev in self._tabs():
            self.win._goto_tab(prev)
        self.win._log(f"🤖 助手新建标签页 {key}")
        return {"tab": key, "current_tab": str(self.win._current_tab)}

    def op_tabs_update(self, params: dict) -> dict:
        key, tab = self._tab(params.get("tab"))
        self._require_writable(key, tab)
        cloud_locked = bool(self.win._is_cloud_order_tab(tab))
        fields = {k: params[k] for k in DELIVERY_FIELDS if k in params}
        if not fields:
            raise AgentOpError("invalid", f"没有可改的字段（可用: {', '.join(DELIVERY_FIELDS)}）")
        if cloud_locked and any(k != "remark" for k in fields):
            raise AgentOpError("cloud_locked", "这是云端订单的标签页，附加服务与归属由下单方决定，不能改")

        if "delivery_enabled" in fields:
            tab.delivery_enabled = bool(fields["delivery_enabled"])
        if "delivery_location" in fields:
            loc = _as_str(fields["delivery_location"], "delivery_location")
            if loc and loc not in self.cfg.delivery_percentages:
                raise AgentOpError("invalid", f"未知派送地点: {loc}（可选: "
                                              f"{', '.join(self.cfg.delivery_percentages)}）")
            tab.delivery_location = loc
        if "urgency" in fields:
            urg = _as_str(fields["urgency"], "urgency")
            if urg not in self.cfg.urgency_prices:
                raise AgentOpError("invalid", f"未知加急档: {urg}（可选: "
                                              f"{', '.join(self.cfg.urgency_prices)}）")
            tab.urgency = urg
        if "cover_page" in fields:
            tab.cover_page = bool(fields["cover_page"])
        if "cover_page_price" in fields:
            price = float(fields["cover_page_price"])
            if price < 0 or price > 100:
                raise AgentOpError("invalid", "cover_page_price 超出合理范围（0~100）")
            tab.cover_page_price = round(price, 2)
        if "owner_name" in fields:
            owner = _as_str(fields["owner_name"], "owner_name").strip()
            members = self.win._member_names()
            if owner and members and owner not in members:
                raise AgentOpError("invalid", f"未知成员: {owner}（可选: {', '.join(members)}）")
            tab.owner_name = owner
        if "is_admin_print" in fields:
            tab.is_admin_print = bool(fields["is_admin_print"])
        if "remark" in fields:
            tab.remark = _as_str(fields["remark"], "remark")

        if self._is_current(key):
            self.win._sync_tab_settings_to_ui(tab)
            self.win._refresh_tab_display()
        self.win._save_config()
        self.win._log(f"🤖 助手修改标签页 {key} 设置: "
                      f"{', '.join(sorted(fields))}")
        return self._brief(key, tab)

    def op_tabs_delete(self, params: dict) -> dict:
        key, tab = self._tab(params.get("tab"))
        if getattr(tab, "frozen", False) and not params.get("force"):
            raise AgentOpError("frozen", f"标签页 {key} 已冻结（已出过订单），"
                                         f"确实要删请传 force:true")
        try:
            inflight = self.win._tab_jobs_in_flight(key)
        except Exception:
            inflight = []
        if inflight:
            raise AgentOpError("busy", f"标签页 {key} 正在打印，不能删除")
        removed = self.win._remove_tabs([key], reason="智能助手删除标签页")
        try:
            self.win._rebuild_table()
            self.win._refresh_tab_display()
            self.win._sync_edit_enabled(False)
        except Exception as e:
            logger.warning(f"[agent-api] 删除后刷新界面失败: {e}")
        self.win._log(f"🤖 助手删除标签页 {key}")
        return {"removed": removed, "current_tab": str(self.win._current_tab)}

    def op_tabs_clear(self, params: dict) -> dict:
        key, tab = self._tab(params.get("tab"))
        self._require_writable(key, tab)
        removed = len(tab.jobs)
        tab.jobs = []
        self._save(key, tab.jobs)
        if self._is_current(key):
            self.win._rebuild_table()
            self.win._refresh_tab_display()
        self.win._log(f"🤖 助手清空标签页 {key} 的 {removed} 个文件")
        return {"removed": removed, "tab": key}

    def op_tabs_print(self, params: dict) -> dict:
        if not params.get("confirm"):
            raise AgentOpError("confirm_required",
                               "开始打印必须显式传 confirm:true（会在打印机上真的出纸）")
        key, tab = self._tab(params.get("tab"))
        win = self.win
        if getattr(tab, "frozen", False):
            raise AgentOpError("frozen", f"标签页 {key} 已打印过（冻结），不能重复打印")
        if not tab.jobs:
            raise AgentOpError("invalid", f"标签页 {key} 里没有文件")
        if not win._has_members():
            raise AgentOpError("no_member", "本机还没有成员（订单必须归属某个成员），"
                                            "请在 收支统计 → 设置 里添加")
        try:
            busy = bool(win._stack_busy())
        except Exception:
            busy = False
        if busy or getattr(win, "_print_starting", False):
            raise AgentOpError("busy", "打印机上还有任务在跑（或正在取号），稍后再试")
        pending = win._pending_page_count_stats(tab.jobs)
        if pending:
            raise AgentOpError("pages_pending", f"还有 {len(pending)} 个文件正在统计页数"
                                                f"（{'、'.join(pending[:3])}），等一下再打")
        for j in tab.jobs:                     # 先自己校验，别让 _start_print_worker 弹框
            total = int(getattr(j, "page_count", 0) or 0)
            raw = (getattr(j, "page_range", "") or "").strip()
            if total > 0 and raw and not pc._parse_range_parts(raw, total):
                raise AgentOpError("invalid", f"文件「{getattr(j, 'display_name', '')}」"
                                              f"的页码范围无效: {raw}")

        if not self._is_current(key):
            self.win._goto_tab(key)
        ok = win._start_print_worker(list(tab.jobs), tab_key=key, interactive=False)
        if not ok:
            raise AgentOpError("print_blocked", "打印没有启动（可能忙/被拦），详见程序日志")
        self.win._log(f"🤖 助手开始打印标签页 {key}（{len(tab.jobs)} 个文件）")
        return {"started": True, "tab": key, "files": len(tab.jobs)}

    # ══════════════════════════════════════════════════════════════════
    # files
    # ══════════════════════════════════════════════════════════════════

    def _check_paths(self, raw) -> list:
        if not isinstance(raw, list) or not raw:
            raise AgentOpError("invalid", "paths 必须是非空字符串数组")
        if len(raw) > MAX_FILES_PER_CALL:
            raise AgentOpError("invalid", f"一次最多 {MAX_FILES_PER_CALL} 个文件")
        out, bad = [], []
        seen = set()
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise AgentOpError("invalid", f"paths 里有非字符串项: {item!r}")
            path = os.path.abspath(item.strip().strip('"'))
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            if not os.path.isfile(path):
                bad.append(f"{path}（不存在或不是文件）")
                continue
            ext = _norm_ext(path)
            if ext in HTML_EXTS:
                bad.append(f"{os.path.basename(path)}（本工具已不支持 HTML，请另存为 PDF/DOCX）")
                continue
            if ext not in ALLOWED_EXTS:
                bad.append(f"{os.path.basename(path)}（不支持的格式 {ext or '无扩展名'}）")
                continue
            out.append(path)
        if bad:
            raise AgentOpError("invalid", "这些文件不能加入: " + "；".join(bad[:5]))
        return out

    def op_files_add(self, params: dict) -> dict:
        paths = self._check_paths(params.get("paths"))
        options = params.get("options") or {}
        self._check_job_options(options)

        win = self.win
        prev = str(win._current_tab)
        if params.get("new_tab"):
            key = win._new_tab()
        else:
            key, _tab = self._tab(params.get("tab"))
            if not self._is_current(key) and not win._goto_tab(key):
                raise AgentOpError("not_found", f"标签页 {key} 打不开")

        tab = self._tabs().get(key)
        if tab is None:
            raise AgentOpError("not_found", f"标签页 {key} 不存在")
        if getattr(tab, "frozen", False):
            raise AgentOpError("frozen", f"标签页 {key} 已冻结（已出过订单），不能加文件")

        before = list(tab.jobs)
        try:
            win._add_files_to_table(paths)
        except Exception as e:
            logger.exception(f"[agent-api] 添加文件异常: {e}")
            raise AgentOpError("internal", f"添加文件失败: {e}")
        jobs = win._get_current_jobs()
        added = [j for j in jobs if all(j is not b for b in before)]
        if not added:
            raise AgentOpError("rejected",
                               "没有文件被加入（可能已在当前标签页里，或全部被拒；详见程序日志）")
        try:
            if options:
                for job in added:
                    self._apply_job_options(job, options)
                win._set_current_jobs(jobs)
                rows = [i for i, j in enumerate(jobs) if any(j is a for a in added)]
                self._refresh_rows(key, jobs, rows)
        except AgentOpError:
            # 参数不合法就别留半个文件在列表里：把这次加进来的原样撤掉
            keep = [j for j in jobs if all(j is not a for a in added)]
            win._set_current_jobs(keep)
            win._rebuild_table()
            raise
        if not params.get("switch", True) and prev in self._tabs() and prev != key:
            win._goto_tab(prev)
        names = "、".join((getattr(j, "display_name", "") or os.path.basename(j.file_path))
                         for j in added[:3])
        more = f" 等 {len(added)} 个" if len(added) > 3 else ""
        win._log(f"🤖 助手往标签页 {key} 加入 {names}{more}")
        return {"tab": key, "added": len(added), "requested": len(paths),
                "jobs": [self._job_brief(i, j) for i, j in enumerate(jobs) if any(j is a for a in added)],
                "brief": self._brief(key, tab)}

    def op_files_update(self, params: dict) -> dict:
        key, tab = self._tab(params.get("tab"))
        self._require_writable(key, tab)
        index = _as_int(params.get("index"), "index")
        if not (0 <= index < len(tab.jobs)):
            raise AgentOpError("not_found", f"标签页 {key} 没有第 {index} 个文件"
                                            f"（共 {len(tab.jobs)} 个，序号从 0 开始）")
        options = {k: params[k] for k in JOB_FIELDS if k in params}
        if not options:
            raise AgentOpError("invalid", f"没有可改的字段（可用: {', '.join(JOB_FIELDS)}）")
        self._check_job_options(options)
        job = tab.jobs[index]
        self._apply_job_options(job, options)
        self._save(key, tab.jobs)
        self._refresh_rows(key, tab.jobs, [index])
        name = getattr(job, "display_name", "") or os.path.basename(job.file_path or "")
        self.win._log(f"🤖 助手修改标签页 {key} 第 {index} 个文件（{name}）: "
                      f"{', '.join(sorted(options))}")
        return self._job_brief(index, job)

    def op_files_remove(self, params: dict) -> dict:
        key, tab = self._tab(params.get("tab"))
        self._require_writable(key, tab)
        index = _as_int(params.get("index"), "index")
        if not (0 <= index < len(tab.jobs)):
            raise AgentOpError("not_found", f"标签页 {key} 没有第 {index} 个文件")
        job = tab.jobs.pop(index)
        self._save(key, tab.jobs)
        if self._is_current(key):
            self.win._rebuild_table()
            self.win._update_total_cost()
        name = getattr(job, "display_name", "") or os.path.basename(job.file_path or "")
        self.win._log(f"🤖 助手从标签页 {key} 移除文件（{name}）")
        return {"tab": key, "removed": name, "files": len(tab.jobs)}

    # ══════════════════════════════════════════════════════════════════
    # 云端口（只读）：给小印看云端订单数据
    # ══════════════════════════════════════════════════════════════════

    # 这些字段原样透传给后端 /api/printer/orders（后端负责校验与拼 SQL）
    CLOUD_QUERY_FIELDS = ("order", "status", "source", "owner", "file", "since", "until")

    def _cloud_ready(self):
        """取云端口要用的 (client, api_url, token)；没连云端就报 cloud_off。

        token 一律用 `auth_token`（本机设备 token 优先），**不是** `client.token`
        （全局口令，2026-11-04 轮换后已失效 → 取号/上报 403 的根因）。
        """
        client = getattr(self.win, "_cloud_client", None)
        if client is None:
            raise AgentOpError("cloud_off", "云打印未启用（先在设置里连接云端）")
        api_url = str(getattr(client, "api_url", "") or "").rstrip("/")
        token = str(getattr(client, "auth_token", "") or "")
        if not api_url or not token:
            raise AgentOpError("cloud_off", "云端未配置（api_url 或设备 token 为空）")
        return client, api_url, token

    def _http_get_json(self, url: str, params: dict, token: str,
                       client_id: str = "", timeout: float = 20.0) -> dict:
        """在主线程里发一次只读 GET：网络在后台线程跑，主线程一边泵事件一边等。

        为什么不在主线程直接阻塞 requests：本 op 由 QTimer 泵在 GUI 主线程执行，
        网络卡住时整个窗口会"未响应"（用户以为程序挂了）。取号/上报路径也是同样写法。
        网络层失败一律抛 cloud_timeout，云端明确拒绝抛 cloud_error。
        """
        from cloud_client import auth_headers     # 晚导入：避免 op 模块加载就拖起 Qt

        box: dict = {}

        def _worker():
            try:
                resp = http_requests.get(url, headers=auth_headers(token, client_id),
                                         params=params, timeout=timeout)
                box["status"] = resp.status_code
                try:
                    box["data"] = resp.json()
                except ValueError:
                    box["text"] = (resp.text or "")[:300]
            except Exception as exc:              # 网络层任何失败都归为"连不上"
                box["error"] = f"{type(exc).__name__}: {exc}"

        th = threading.Thread(target=_worker, name="agent-cloud-get", daemon=True)
        th.start()
        from PySide6.QtCore import QCoreApplication, QEventLoop
        deadline = time.monotonic() + timeout + 2.0
        while th.is_alive() and time.monotonic() < deadline:
            QCoreApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            time.sleep(0.02)
        if th.is_alive():
            raise AgentOpError("cloud_timeout", f"云端 {timeout:.0f}s 内没有响应（网络不通？）")
        if "error" in box:
            raise AgentOpError("cloud_timeout", f"连不上云端：{box['error']}")
        status = int(box.get("status") or 0)
        data = box.get("data")
        if not isinstance(data, dict):
            raise AgentOpError("cloud_error",
                              f"云端返回非 JSON（HTTP {status}）：{box.get('text', '')}")
        if status >= 400 or not data.get("success", False):
            raise AgentOpError("cloud_error",
                              str(data.get("message") or f"云端 HTTP {status}"))
        return data

    def op_cloud_orders(self, params: dict) -> dict:
        """查看云端订单数据（只读、**不限时间**）：默认全量历史，可按订单号/状态/来源/
        归属/文件名/时间段过滤。

        返回 `{orders, total, limit, offset, since, until, summary}`；每张订单卡的字段与
        界面「最近订单」一致（页数/张数/金额同一份口径，见后端 `_order_view_dict`）。
        """
        client, api_url, token = self._cloud_ready()
        query: dict = {}
        for key in self.CLOUD_QUERY_FIELDS:
            val = params.get(key)
            if val is None:
                continue
            text = str(val).strip()
            if text:
                query[key] = text
        for key in ("limit", "offset"):
            if params.get(key) is not None:
                query[key] = str(_as_int(params.get(key), key))
        data = self._http_get_json(f"{api_url}/api/printer/orders", query, token,
                                   str(getattr(client, "client_id", "") or ""))
        return {
            "orders": data.get("orders") or [],
            "total": int(data.get("total") or 0),
            "limit": int(data.get("limit") or 0),
            "offset": int(data.get("offset") or 0),
            "since": str(data.get("since") or ""),
            "until": str(data.get("until") or ""),
            "summary": data.get("summary") or {},
        }

    # ══════════════════════════════════════════════════════════════════
    # 单文件参数（与界面编辑面板同一口径）
    # ══════════════════════════════════════════════════════════════════

    def _check_job_options(self, options) -> None:
        if options in (None, {}):
            return
        if not isinstance(options, dict):
            raise AgentOpError("invalid", "options 必须是 JSON 对象")
        unknown = set(options) - set(JOB_FIELDS)
        if unknown:
            raise AgentOpError("invalid", f"未知参数: {', '.join(sorted(unknown))}"
                                          f"（可用: {', '.join(JOB_FIELDS)}）")
        if "copies" in options:
            copies = _as_int(options["copies"], "copies")
            if not 1 <= copies <= 99:
                raise AgentOpError("invalid", "copies 只能是 1~99")
        if "duplex" in options:
            if str(options["duplex"]).lower() not in ("on", "off"):
                raise AgentOpError("invalid", "duplex 只能是 on / off")
        if "duplex_mode" in options:
            if str(options["duplex_mode"]) not in ("long-edge", "short-edge", ""):
                raise AgentOpError("invalid", "duplex_mode 只能是 long-edge / short-edge")
        if "engine" in options and str(options["engine"]) not in ENGINES:
            raise AgentOpError("invalid", f"engine 只能是 {', '.join(sorted(ENGINES))}")
        if "dpi" in options:
            dpi = _as_int(options["dpi"], "dpi")
            if dpi not in DPI_CHOICES:
                raise AgentOpError("invalid", f"dpi 只能是 {', '.join(map(str, sorted(DPI_CHOICES)))}")
        if "image_orientation" in options and \
                str(options["image_orientation"]) not in ORIENT_CHOICES:
            raise AgentOpError("invalid", "image_orientation 只能是 auto / landscape / portrait")
        if "page_range" in options and not isinstance(options["page_range"], str):
            raise AgentOpError("invalid", "page_range 必须是字符串（如 \"1-3,5\"；空串=全部页）")
        if "simplex_pages" in options and not isinstance(options["simplex_pages"], str):
            raise AgentOpError("invalid", "simplex_pages 必须是字符串（如 \"1\"）")
        for k in ("display_name", "remark"):
            if k in options and not isinstance(options[k], str):
                raise AgentOpError("invalid", f"{k} 必须是字符串")

    def _apply_job_options(self, job, options: dict) -> None:
        """把 options 写进 job，并做与界面编辑面板相同的归一化。

        归一化不是可选的：
        · 图片 / 实际只打 1 页 ⇒ 双面一律 off（单页硬闸门，硬规矩 1）；
        · 封面单独打印只有双面才有意义，且必须过 `check_simplex_pages` 同一套判据；
        · 改了引擎/DPI/图片方向 ⇒ 作废已转换的 PDF 缓存，否则打出来还是旧排版。
        """
        if "copies" in options:
            job.copies = _as_int(options["copies"], "copies")
        if "display_name" in options:
            name = options["display_name"].strip()
            if name:
                job.display_name = name
        if "remark" in options:
            job.remark = options["remark"]

        rebuild_pdf = False
        if "page_range" in options:
            raw = _normalize_range_text(options["page_range"])
            total = int(getattr(job, "page_count", 0) or 0)
            if total > 0:
                parts = pc._parse_range_parts(raw, total)
                if raw and not parts:
                    raise AgentOpError("invalid", f"页码范围无效: {options['page_range']}（共 {total} 页）")
                job.page_range = "" if (not raw or parts == set(range(1, total + 1))) \
                    else pc.format_page_selection(parts)
            else:
                job.page_range = raw           # 页数还没数出来：先存，打印前会再校验
        if "engine" in options:
            engine = str(options["engine"])
            if _norm_ext(job.file_path or "") in (".doc", ".docx") and engine != job.engine:
                job.engine = engine
                rebuild_pdf = True
        if "dpi" in options:
            dpi = _as_int(options["dpi"], "dpi")
            if dpi != int(getattr(job, "dpi", 0) or 0):
                job.dpi = dpi
                rebuild_pdf = True
        if "image_orientation" in options:
            orient = str(options["image_orientation"])
            if _norm_ext(job.file_path or "") in IMAGE_EXTS and orient != job.image_orientation:
                job.image_orientation = orient
                rebuild_pdf = True

        if "duplex" in options:
            job.duplex = "on" if str(options["duplex"]).lower() == "on" else "off"
        if "duplex_mode" in options and job.duplex == "on":
            job.duplex_mode = str(options["duplex_mode"])
        # 单页硬闸门（图片 / 实际只打 1 页 ⇒ 单面），与 _auto_apply_edit 同一条
        is_img = _norm_ext(job.file_path or "") in IMAGE_EXTS
        if is_img or pc._count_pages_in_range(job.page_range or "",
                                              job.page_count or 0) == 1:
            job.duplex = "off"

        if "simplex_enabled" in options or "simplex_pages" in options:
            want = bool(options.get("simplex_enabled", getattr(job, "simplex_enabled", False)))
            raw = str(options.get("simplex_pages", getattr(job, "simplex_pages", "")) or "")
            if want and job.duplex == "on":
                normalized, reason = pc.check_simplex_pages(job.page_range or "",
                                                            job.page_count or 0, raw)
                if reason:
                    raise AgentOpError("invalid", f"封面单独打印的页码无效: {reason}")
                if not normalized:
                    raise AgentOpError("invalid", "封面单独打印要填页码（例如 \"1\"）")
                job.simplex_pages = normalized
                job.simplex_enabled = True
            else:
                job.simplex_enabled = False
                if raw:
                    job.simplex_pages = raw   # 与界面一致：取消勾选只关开关，留住页码
        if rebuild_pdf:
            job.cached_pdf = ""


def _normalize_range_text(raw: str) -> str:
    """页码输入归一化：全角逗号/顿号/空格一律当分隔符（与 printer_config 同口径）。"""
    text = (raw or "").strip()
    for ch in ("、", "，", "；", " "):
        text = text.replace(ch, ",")
    return text


_OPS = {
    "status": AgentOps.op_status,
    "tabs.list": AgentOps.op_tabs_list,
    "tabs.get": AgentOps.op_tabs_get,
    "tabs.create": AgentOps.op_tabs_create,
    "tabs.update": AgentOps.op_tabs_update,
    "tabs.delete": AgentOps.op_tabs_delete,
    "tabs.clear": AgentOps.op_tabs_clear,
    "tabs.print": AgentOps.op_tabs_print,
    "files.add": AgentOps.op_files_add,
    "files.update": AgentOps.op_files_update,
    "files.remove": AgentOps.op_files_remove,
    "cloud.orders": AgentOps.op_cloud_orders,
}
