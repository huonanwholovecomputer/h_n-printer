# -*- coding: utf-8 -*-
"""tray_icon.py — 系统托盘图标 + 右键菜单（后台常驻入口，4.6.0 起）

为什么要它：云打印要「后台常驻」——操作员关掉主窗口后，程序仍得收云端任务、
跑外部打印监视、按预约时间出纸。旧版本「关闭窗口 = 退出进程」把这条链路一起关了。

本模块**只做界面**（图标 + 右键菜单），所有动作都回调 MainWindow 上已有的入口，
不复制任何业务逻辑 —— 否则就会出现「文件菜单一份、托盘一份」的第二套真相。

右键菜单（2026-11 用户指定，顺序即下面 addAction 的顺序）：
  · 显示应用   —— 把主窗口从托盘叫回来（双击托盘图标同效）
  · 云端…      —— 云端连接设置（= 文件 → 云端…）
  · √本地监视  —— 外部打印监视开关（= 文件 → 外部打印监视，勾选态共用同一个 QAction）
  · 收支清算   —— 打开收支清算/收支统计页（= 文件 → 收支清算）
  · 退出       —— 真正的退出（关窗口只是收回托盘）

要点：
  1. 勾选态**只有一个真相**：文件菜单的 `_ext_print_action`。托盘这边每 1.5s 把
     `setChecked` 同步过去（带 blockSignals，不会形成信号回环），用户不管从哪边切，
     两边显示一致；`_on_external_watch_toggled` 也只会被触发一次。
  2. 系统托盘不可用（`QSystemTrayIcon.isSystemTrayAvailable()` 为 False，常见于远程桌面
     会话被裁剪、或离屏测试）时 `start()` 返回 False → MainWindow 退回旧行为：
     关窗口就是退出，绝不把程序藏进一个不存在的托盘里变成「进程还在但找不到」。
  3. 退出时 `stop()` 先 `hide()` 图标：Windows 通知区常有「进程退了、图标还赖着，
     鼠标划过才消失」的残影。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

logger = logging.getLogger(__name__)

# 托盘图标提示文案（悬停可见）。版本号由 MainWindow 传入，避免在这里再写一遍。
TRAY_TOOLTIP = "HN 本地打印工具 v{version}\n双击显示主窗口，右键查看更多"

# 「已收到托盘」的气泡只弹一次（每会话）：第一次关窗口告诉用户程序没退；
# 之后每次关窗都弹就是在骚扰 —— Windows 通知区本来就有图标在那儿。
BALLOON_TITLE = "HN 打印工具仍在后台运行"
BALLOON_BODY = ("云端接单、外部打印监视、预约出纸都还在跑。\n"
                "要彻底退出：右键托盘图标 → 退出。")

# 本地监视开关的气泡反馈（窗口藏起来时日志区看不见，勾了没勾上得有个回声）
WATCH_ON_BODY = "已开始检测绕过本工具、直接发到打印机的作业（只记账，不阻断出纸）"
WATCH_OFF_BODY = "已停止检测绕过本工具直接打印的作业"

# 勾选态与文件菜单同步的周期（毫秒）。文件菜单那条 QAction 是唯一真相，
# 这里只做「读过来」——不监听 toggled 是为了避开两边互相 setChecked 的回环。
SYNC_INTERVAL_MS = 1500


class TrayController(QObject):
    """主窗口的托盘代理。

    生命周期跟着 MainWindow：`window = MainWindow` 时本对象以窗口为父，
    窗口析构即析构；`stop()` 只在真正退出时调用（hide 通知区图标）。
    """

    def __init__(self, window, icon: QIcon | None, version: str = "") -> None:
        super().__init__(window)
        self._window = window
        self._shown_balloon = False
        self._started = False

        self._tray = QSystemTrayIcon(icon if icon is not None else QIcon(), window)
        self._tray.setToolTip(TRAY_TOOLTIP.format(version=version) if version
                              else "HN 本地打印工具")
        self._menu = self._build_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)

        # 勾选态同步（读文件菜单那一份真相）
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(SYNC_INTERVAL_MS)
        self._sync_timer.timeout.connect(self.sync_watch_state)

    # ──────────────────────────── 菜单 ────────────────────────────

    def _build_menu(self) -> QMenu:
        menu = QMenu(self._window)

        # ① 显示应用 —— 与双击托盘图标同效
        self._act_show = QAction("显示应用", menu)
        self._act_show.setToolTip("把主窗口叫回前台")
        self._act_show.triggered.connect(self.show_window)
        menu.addAction(self._act_show)

        # ② 云端… —— 复用主窗口的云端连接设置对话框
        self._act_cloud = QAction("云端…", menu)
        self._act_cloud.setToolTip("云端服务器地址 / 令牌 / 是否接收云端订单")
        self._act_cloud.triggered.connect(self._on_cloud_settings)
        menu.addAction(self._act_cloud)

        # ③ 本地监视 —— 与「文件 → 外部打印监视」共用一个 QAction（同一个真相）
        self._act_watch = QAction("本地监视", menu)
        self._act_watch.setCheckable(True)
        self._act_watch.setToolTip("本地监视：检测绕过本工具直接发到打印机的作业并记账"
                                   "（只记账，不阻断出纸）")
        self._act_watch.toggled.connect(self._on_watch_toggled)
        menu.addAction(self._act_watch)
        # 初值按文件菜单的现状来（启动恢复监视线程在 1200ms 后，之后由定时器跟）
        self.sync_watch_state()

        # ④ 收支清算
        self._act_finance = QAction("收支清算", menu)
        self._act_finance.setToolTip("打开收支清算 / 收支统计页面（浏览器）")
        self._act_finance.triggered.connect(self._on_finance)
        menu.addAction(self._act_finance)

        menu.addSeparator()

        # ⑤ 退出 —— 只有这里是「真退出」；关窗口是收回托盘
        self._act_quit = QAction("退出", menu)
        self._act_quit.setToolTip("结束程序（关窗口只是收回托盘，程序继续在后台跑）")
        self._act_quit.triggered.connect(self._on_quit)
        menu.addAction(self._act_quit)

        return menu

    # ──────────────────────────── 启停 ────────────────────────────

    def is_available(self) -> bool:
        """系统托盘是否可用（离屏测试 / 被裁剪的远端会话里为 False）。"""
        try:
            return bool(QSystemTrayIcon.isSystemTrayAvailable())
        except Exception:  # 没有 QApplication 等极端情形
            return False

    def start(self) -> bool:
        """把图标挂到通知区。不可用时返回 False（调用方据此退回「关窗即退出」）。"""
        if not self.is_available():
            logger.info("系统托盘不可用：关闭窗口仍按旧行为退出")
            return False
        self._tray.show()
        self._sync_timer.start()
        self._started = True
        return True

    def stop(self) -> None:
        """退出前收起图标（避免通知区残影）并停掉同步定时器。"""
        self._sync_timer.stop()
        try:
            self._tray.hide()
        except Exception as e:
            logger.debug(f"隐藏托盘图标失败: {e}")
        self._started = False

    def is_started(self) -> bool:
        return self._started

    # ──────────────────────────── 动作 ────────────────────────────

    def show_window(self) -> None:
        """显示应用：恢复主窗口（隐藏 / 最小化 / 背后都拉回前台）。"""
        restore = getattr(self._window, "_restore_from_tray", None)
        if callable(restore):
            restore()

    def _on_activated(self, reason) -> None:
        """单击 / 双击图标 → 显示应用（Windows 习惯双击，两种都接）。"""
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def _on_cloud_settings(self) -> None:
        # 不强行把主窗口整块弹起来：主窗口在托盘里时 gui._on_cloud_settings 会自己开
        # 独立对话框（父窗口隐藏的模态框没有任务栏按钮，抢不到前台就成了看不见的死锁）。
        open_settings = getattr(self._window, "_on_cloud_settings", None)
        if callable(open_settings):
            open_settings()

    def _on_watch_toggled(self, checked: bool) -> None:
        """托盘勾选 → 写回文件菜单那一条 QAction（唯一真相），由它起停监视线程。"""
        action = getattr(self._window, "_ext_print_action", None)
        if action is not None:
            # 已经是这个状态就不动（避免多余的一次 start/stop）
            if bool(action.isChecked()) != bool(checked):
                action.setChecked(bool(checked))
        self._balloon(WATCH_ON_BODY if checked else WATCH_OFF_BODY)

    def _on_finance(self) -> None:
        """收支清算：直接开浏览器页面，不必先把主窗口叫回来。"""
        open_finance = getattr(self._window, "_on_open_finance", None)
        if callable(open_finance):
            open_finance()

    def _on_quit(self) -> None:
        quit_app = getattr(self._window, "_quit_app", None)
        if callable(quit_app):
            quit_app()

    # ──────────────────────── 状态同步 / 气泡 ────────────────────────

    def sync_watch_state(self) -> None:
        """把文件菜单的「外部打印监视」勾选态读过来（单向，不发射信号）。"""
        action = getattr(self._window, "_ext_print_action", None)
        if action is None:
            return
        want = bool(action.isChecked())
        if bool(self._act_watch.isChecked()) == want:
            return
        self._act_watch.blockSignals(True)
        try:
            self._act_watch.setChecked(want)
        finally:
            self._act_watch.blockSignals(False)

    def notify_hidden(self) -> None:
        """关窗口收到托盘时的第一次提示（每会话一次）。

        只在**真的弹出来**之后才记「弹过了」—— 托盘还没 start() 时调用不能把额度用掉，
        否则先起窗口后起托盘的那种顺序会让用户一次提示都收不到。
        """
        if self._shown_balloon or not self._started:
            return
        self._shown_balloon = True
        self._balloon(BALLOON_BODY, title=BALLOON_TITLE)

    def _balloon(self, body: str, title: str = "HN 打印工具") -> None:
        if not self._started:
            return
        try:
            # 无图标参数 → 用托盘图标
            self._tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 4000)
        except Exception as e:
            logger.debug(f"托盘气泡提示失败: {e}")
