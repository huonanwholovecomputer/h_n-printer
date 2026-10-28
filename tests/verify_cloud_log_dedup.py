# -*- coding: utf-8 -*-
"""云端日志「同一事件只写一行」验收（2026-10-28 用户要求：重复云端日志一起收敛）

现场（2026-10-28 21:40，订单 #822）：同一秒出现两条**一模一样**的
「☁ 订单 #822 已被用户取消」—— 不是后端发了两次，而是本机两处各写了一遍：
  · cloud_client.py 收到 order_canceled → status_message（GUI 转成日志）
  · gui.py 的 _on_cloud_order_canceled 自己又 _log 一次
同一族还有两对（文案不同但属同一事件，现场同样读成"收到了两次"）：
  · 「☁ 收到云任务 #N: f (1份, 单面)」 ⟷ 「☁ 收到云端任务 #N: f ⚡无障碍」
    外加 HTTP 拉取通道的「☁ HTTP 拉取到任务 #N: f」
  · 「☁ 下载完成 #N: f (bytes)」 ⟷ 「☁ 云端任务 #N 下载完成 ⚡无障碍」

收敛口径：**传输层（cloud_client）一处输出，GUI 只做界面动作、不再写日志** ——
所有事实（份数/单双面、⚡无障碍、⏰预约、文件名/字节数、通道后缀）都并入那一行。
推送与 HTTP 拉取是两个互斥通道，各有一处 emit 是**同一个事件**的两条可能路径，不算重复。

用法: python tests/verify_cloud_log_dedup.py   （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CC_PATH = os.path.join(REPO, "local_print_tool", "cloud_client.py")
GUI_PATH = os.path.join(REPO, "local_print_tool", "gui.py")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


CC = open(CC_PATH, encoding="utf-8").read()
GUI = open(GUI_PATH, encoding="utf-8").read()


def _cancel_single(cc, gui):
    """① 订单取消/人工订正：cloud_client 一处；gui 不再自己写第二行。"""
    return (cc.count("☁ 订单 #{order_id} 已被用户取消") == 1
            and 'self._log(f"☁ 订单 #{order_id} {reason}")' not in gui)


def _arrive_single(cc, gui):
    """② 收到任务：cloud_client 两处（推送 / HTTP 拉取，互斥通道，同一段文案）；GUI 零处。"""
    return (cc.count("☁ 收到云端任务 #") == 2
            and "☁ 收到云任务 #" not in cc          # 旧的推送文案已并入同一条
            and "HTTP 拉取到任务" not in cc          # 旧的拉取文案已并入同一条
            and "☁ 收到云端任务 #" not in gui)


def _download_single(cc, gui):
    """③ 下载完成：cloud_client 一处；GUI 的「云端任务 #N 下载完成」已删。"""
    return (cc.count("☁ 下载完成 #") == 1
            and "云端任务 #{task.task_id} 下载完成" not in gui)


section("① 订单取消 / 人工订正：只有一行")
check("★ cloud_client 保留唯一一行，gui 不再 _log 同一句", _cancel_single(CC, GUI))
check("回归：gui 仍在做清理动作（不是把整段删掉）",
      "_purge_canceled_print_state(order_id, task_ids)" in GUI
      and "self._cloud_canceled_orders.add(order_id)" in GUI)

section("② 收到云端任务：只有一行（推送 / HTTP 拉取同一段文案）")
check("★ cloud_client 两处 emit（推送 + 拉取）用同一条文案，GUI 不再写", _arrive_single(CC, GUI))
check("这一行带齐现场需要的事实（份数 / 单双面 / ⚡无障碍 / ⏰预约 / 通道后缀）",
      "({task.copies}份, {'双面' if task.duplex == 'on' else '单面'})" in CC
      and '(" ⚡无障碍" if task.auto_print else "")' in CC
      and '(" ⏰预约" if (task.auto_print' in CC
      and '"（HTTP 拉取）"' in CC)
check("回归：gui 的预约/无障碍分派逻辑仍在（只删了日志行）",
      "self._register_scheduled_task(task)" in GUI and "self._enqueue_auto_print_task(task)" in GUI)

section("③ 下载完成：只有一行")
check("★ cloud_client 保留唯一一行（带文件名 / 字节数 / ⚡无障碍），GUI 的重复行已删",
      _download_single(CC, GUI))
check("回归：gui 的 ready 分支仍在（只删了日志行）",
      'elif task.status == "ready":' in GUI and "self._cloud_task_window.add_task(task)" in GUI)

section("④ 判据自证（把这些行写回去，判据必须变红）")

_dup_cancel = '        self._log(f"☁ 订单 #{order_id} {reason}")\n'
_m1 = GUI.replace("        self._cloud_canceled_orders.add(order_id)",
                  _dup_cancel + "        self._cloud_canceled_orders.add(order_id)", 1)
check("B1 取消日志写回 gui（同一秒两条）→ 判据变红",
      _m1 != GUI and not _cancel_single(CC, _m1))

_dup_arrive = '        self._log(f"☁ 收到云端任务 #{task.task_id}: {task.file_name}")\n'
_m2 = GUI.replace("        if is_scheduled:",
                  _dup_arrive + "        if is_scheduled:", 1)
check("B2 收到任务写回 gui（两条）→ 判据变红",
      _m2 != GUI and _arrive_single(CC, GUI) and not _arrive_single(CC, _m2))

_m3 = CC.replace("☁ 收到云端任务 #{task.task_id}", "☁ 收到云任务 #{task.task_id}")
check("B3 文案退回旧的「收到云任务」（不再与其它通道同源）→ 判据变红",
      _m3 != CC and not _arrive_single(_m3, GUI))

_dup_dl = '            self._log(f"☁ 云端任务 #{task.task_id} 下载完成")\n'
_m4 = GUI.replace('        elif task.status == "ready":',
                  '        elif task.status == "ready":\n' + _dup_dl, 1)
check("B4 下载完成写回 gui（两条）→ 判据变红",
      _m4 != GUI and not _download_single(CC, _m4))

_m5 = CC.replace('f"☁ 下载完成 #{task_id}: {original_name} ({os.path.getsize(dest)} bytes)"',
                 'f"☁ 已落地 #{task_id}"')
check("B5 cloud_client 的「下载完成」整行被换掉 → 判据变红",
      _m5 != CC and not _download_single(_m5, GUI))

print("\n" + "=" * 56)
print("云端日志去重验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
