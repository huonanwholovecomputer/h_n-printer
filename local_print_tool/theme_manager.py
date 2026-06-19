"""
theme_manager.py — 主题管理器
支持浅色/深色双主题，可手动切换或跟随系统设置。
检测 Windows 系统深浅模式，监听系统主题变化实时响应。
"""

from __future__ import annotations

import json
import logging
import os

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

# 主题模式常量
MODE_SYSTEM = "system"
MODE_LIGHT = "light"
MODE_DARK = "dark"

ALL_MODES = [MODE_SYSTEM, MODE_LIGHT, MODE_DARK]

MODE_LABELS = {
    MODE_SYSTEM: "🌐 跟随系统",
    MODE_LIGHT: "☀️ 浅色模式",
    MODE_DARK: "🌙 深色模式",
}


class ThemeManager(QObject):
    """
    主题管理器 — 单例模式。

    职责:
    - 检测系统当前颜色方案（浅色/深色）
    - 支持三种模式：跟随系统、浅色、深色
    - 持久化用户偏好到 JSON 文件
    - 应用 QSS 样式表到 QApplication
    - 监听系统主题变化并自动响应（仅在"跟随系统"模式下）
    """

    theme_changed = Signal(str)  # 参数: "light" | "dark"

    def __init__(self, settings_path: str, parent: QObject | None = None):
        super().__init__(parent)
        self._settings_path = settings_path
        self._mode: str = MODE_SYSTEM
        self._dark_qss: str = ""
        self._light_qss: str = ""
        self._app: QApplication | None = None
        self._load_setting()

    # ---- 公开 API ----

    def init(self, app: QApplication, dark_qss: str, light_qss: str) -> None:
        """初始化：绑定 QApplication、加载样式表、连接系统信号。"""
        self._app = app
        self._dark_qss = dark_qss
        self._light_qss = light_qss

        # 监听系统主题变化
        app.styleHints().colorSchemeChanged.connect(self._on_system_theme_changed)

        # 首次应用主题
        self._apply()

    @property
    def mode(self) -> str:
        """当前模式：'system' | 'light' | 'dark'"""
        return self._mode

    @property
    def effective_theme(self) -> str:
        """当前生效的主题：'light' | 'dark'"""
        if self._mode == MODE_SYSTEM:
            return self._detect_system_theme()
        return self._mode

    def set_mode(self, mode: str) -> None:
        """切换模式并立即生效。"""
        if mode not in ALL_MODES:
            logger.warning(f"无效的主题模式: {mode}")
            return
        if mode == self._mode:
            return
        self._mode = mode
        self._save_setting()
        self._apply()
        logger.info(f"主题模式已切换: {MODE_LABELS.get(mode, mode)}")

    def cycle_mode(self) -> None:
        """在三种模式间循环切换（调试/快捷键用）。"""
        idx = ALL_MODES.index(self._mode)
        next_idx = (idx + 1) % len(ALL_MODES)
        self.set_mode(ALL_MODES[next_idx])

    # ---- 内部方法 ----

    def _apply(self) -> None:
        """应用当前生效的 QSS 样式表。"""
        effective = self.effective_theme
        qss = self._dark_qss if effective == MODE_DARK else self._light_qss
        if self._app:
            self._app.setStyleSheet(qss)
        self.theme_changed.emit(effective)
        logger.debug(f"已应用主题: {effective}")

    def _detect_system_theme(self) -> str:
        """检测系统当前颜色方案。"""
        if self._app is None:
            return MODE_LIGHT
        cs = self._app.styleHints().colorScheme()
        if cs == Qt.ColorScheme.Dark:
            return MODE_DARK
        return MODE_LIGHT

    def _on_system_theme_changed(self) -> None:
        """系统主题变化回调：仅在跟随系统模式下自动切换。"""
        if self._mode == MODE_SYSTEM:
            logger.info("检测到系统主题变化，自动切换")
            self._apply()

    # ---- 持久化 ----

    def _load_setting(self) -> None:
        """从 JSON 文件加载用户偏好。"""
        try:
            if os.path.isfile(self._settings_path):
                with open(self._settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                mode = data.get("theme_mode", MODE_SYSTEM)
                if mode in ALL_MODES:
                    self._mode = mode
                    logger.info(f"已加载主题设置: {MODE_LABELS.get(mode, mode)}")
        except Exception as e:
            logger.warning(f"加载主题设置失败: {e}，使用默认值")

    def _save_setting(self) -> None:
        """保存用户偏好到 JSON 文件。"""
        try:
            os.makedirs(os.path.dirname(self._settings_path) or ".", exist_ok=True)
            with open(self._settings_path, "w", encoding="utf-8") as f:
                json.dump({"theme_mode": self._mode}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存主题设置失败: {e}")
