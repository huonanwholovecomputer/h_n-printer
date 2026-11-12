# -*- coding: utf-8 -*-
"""pass2_watch.py — 「上一批第 2 趟可能还在出纸」的跨批次计时器（纯逻辑）。

背景（2026-11-12 用户现场要求）
------------------------------
手动双面的第 2 趟走 `print_plan_pass(..., 2)`：把整批面一次提交进 spooler 就
**立刻返回**，worker 随即结束、翻纸窗关掉 —— 可纸还在打印机里一张张往外出。

于是有一个真实的时间缝：第 2 趟最后几张还在出来时，操作员（或云端自动派单）
又启动了下一个打印任务，新任务第 1 趟的纸就会插进上一批还没出完的那一叠中间，
两批纸混在一起 —— 翻纸、页序、出纸槽计数全部串掉（现场叫"串页"）。

本模块只负责**计时与窗口判定**，不碰 Qt、不碰打印机：

  · 起点 `start()`：第 2 趟提交进 spooler 那一刻（`time.perf_counter`）；
  · 窗口：该批张数算出的预估耗时（`PrinterProfile.estimate_first_pass`）+ `GRACE_SEC`；
  · 新批次开始第 1 趟之前调 `snapshot()`：仍在窗口内 ⇒ 返回当时状态，由上层弹窗
    请操作员按「确定上一个任务已完成」；已经超窗 ⇒ None，直接放行。

为什么不靠打印机自己回报
------------------------
LaserJet 1020 这类主机型（GDI）打印机只回报「数据已送进端口」，从不回报「纸打完了」；
spooler 也会在纸还没出齐时就把作业清掉（`count_pending_print_jobs` 早已排空）。
能让现场不串页的只有两条腿：**预估窗口** 与 **操作员的眼睛**。

一条硬口径：到点自动放行，但**不当作实测样本**
------------------------------------------------
窗口到点没人确认时，上层照旧开始打印（云端自动单/无人值守不能被这一步卡死），
但那一趟的耗时是"没人看见纸"，不是真实打印时间 ⇒ 绝不能写进速度学习。
只有 `confirm()`（操作员亲手按了「确定上一个任务已完成」）返回的秒数才可信，
上层拿它去 `PrinterProfile.add_speed_sample`。
"""

from __future__ import annotations

import threading
import time

# 预估之外再宽限的时间：预估本身是拟合值（最近 20 次实测），单批偏差几秒很正常，
# 不留余量会把"其实已经出完了"的批次也拦下来，白等。
GRACE_SEC = 30.0


class Pass2Watch:
    """一次"上一批第 2 趟可能还在打"的计时（线程安全，可被 GUI 线程与 worker 同时用）。

    生命周期：worker 提交第 2 趟前 `start()` → 新批次开始第 1 趟前 `snapshot()` →
    操作员按确认 `confirm()` / 到期 / 上层消费完 `release()`。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._t0 = 0.0            # perf_counter 时间戳；0 = 没在计时
        self._sheets = 0
        self._estimate = 0.0
        self._confirmed = threading.Event()
        self._confirmed_elapsed = 0.0

    # ── 写入侧（worker / GUI 线程）────────────────────────────────────────

    def start(self, sheets: int, estimate_sec: float) -> None:
        """第 2 趟刚提交进 spooler：从这里起算。estimate<=0 的批次不会拦人（见 snapshot）。"""
        with self._lock:
            self._t0 = time.perf_counter()
            self._sheets = max(0, int(sheets or 0))
            self._estimate = max(0.0, float(estimate_sec or 0.0))
            self._confirmed.clear()
            self._confirmed_elapsed = 0.0

    def release(self) -> None:
        """计时作废（上层已经消费完：确认或到期）。之后 snapshot() 返回 None。"""
        with self._lock:
            self._clear_locked()

    def confirm(self) -> float:
        """操作员按「确定上一个任务已完成」= 认定上一批第 2 趟打完了。

        返回"已用秒数"（没在计时就 0.0）。重复按只认第一次的秒数 —— 第二次按不能把
        已经记下的实测值改大（那样会越按越慢）。
        """
        with self._lock:
            if self._t0 <= 0:
                return 0.0
            elapsed = max(0.0, time.perf_counter() - self._t0)
            if self._confirmed_elapsed <= 0:
                self._confirmed_elapsed = elapsed
            self._confirmed.set()
            return self._confirmed_elapsed

    # ── 读取侧 ───────────────────────────────────────────────────────────

    def active(self) -> bool:
        with self._lock:
            return self._t0 > 0

    def is_confirmed(self) -> bool:
        return self._confirmed.is_set()

    def confirmed_elapsed(self) -> float:
        with self._lock:
            return float(self._confirmed_elapsed)

    def elapsed(self) -> float:
        with self._lock:
            if self._t0 <= 0:
                return 0.0
            return max(0.0, time.perf_counter() - self._t0)

    def snapshot(self, now: float | None = None) -> dict | None:
        """新批次要开始第 1 趟了：上一批第 2 趟还在窗口内吗？

        返回 None = 不用拦（没在计时 / 没档案算不出预估 / 已经超窗）。
        返回 dict = 要拦，字段：
            sheets      上一批张数
            estimate    上一批预估秒数
            elapsed     已用秒数
            deadline    窗口总长（预估 + 宽限）
            remaining   还剩多少秒自动放行
        """
        with self._lock:
            if self._t0 <= 0 or self._estimate <= 0:
                return None
            t = time.perf_counter() if now is None else float(now)
            elapsed = max(0.0, t - self._t0)
            deadline = self._estimate + GRACE_SEC
            if elapsed > deadline:
                # 早就该出完了：自清，免得下一次又拿一份过期的窗口去拦人。
                self._clear_locked()
                return None
            return {
                "sheets": self._sheets,
                "estimate": self._estimate,
                "elapsed": elapsed,
                "deadline": deadline,
                "remaining": max(0.0, deadline - elapsed),
            }

    def wait_confirmed(self, timeout: float | None = None) -> bool:
        """等操作员确认（worker 里的轮询闸门可以不用它，测试/脚本更方便）。"""
        return self._confirmed.wait(timeout)

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _clear_locked(self) -> None:
        self._t0 = 0.0
        self._sheets = 0
        self._estimate = 0.0
        self._confirmed.clear()
        self._confirmed_elapsed = 0.0
