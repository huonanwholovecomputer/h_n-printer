# -*- coding: utf-8 -*-
"""i18n_zh.py — 把 Qt 自带的英文文案统一成简体中文（确认框 / 标准按钮 / 右键菜单）。

为什么需要这一层：业务文案（标题、正文、我们 addButton 的按钮）早就是中文，但
PySide6 **默认不加载 Qt 翻译器**，Qt 内置的那一层英文会原样画到界面上。现场看到的
「默认 Yes/No/Cancel」全部来自这里：

    QMessageBox 标准按钮            Yes / No / Cancel / OK  →  "&Yes" / "&No" / "Cancel" / "OK"
    QDialogButtonBox 标准按钮       Ok / Save / Cancel      →  "OK" / "Save" / "Cancel"
    QLineEdit / QTextEdit 右键菜单   &Copy / &Paste / Select All
    QInputDialog（补录页数等）        OK / Cancel
    QFileDialog（非原生后端时）        Cancel / Open / Save，筛选框里的 "All Files (*)"

装上 Qt 官方简体中文目录（qtbase_zh_CN.qm）后分别变成「是(&Y)」「否(&N)」「取消」
「确定」「保存」「复制」「粘贴」「全选」。

时机：**必须在创建任何 QWidget 之前调用**。Qt 在控件构造时就取了翻译，晚装不会
重译已经建好的控件（MainWindow 里那些确认框会照样是英文）。调用点见 main.py 里
`app = QApplication(sys.argv)` 之后的第一件事。
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

# qtbase 覆盖 QtCore/QtGui/QtWidgets 的全部内置文案（QMessageBox、QDialogButtonBox、
# 右键菜单、QInputDialog、QFileDialog 都在里面）；qt_zh_CN 是 Qt6 的伞形目录，
# 作为 qtbase 缺失时的兜底（只装其中一个：qtbase 优先，避免后装的把前面的盖掉）。
_TRANSLATION_NAMES = ("qtbase_zh_CN", "qt_zh_CN")

# 模块级保活：QTranslator 一旦被 GC，已安装的翻译立即失效（按钮又变回英文）。
_INSTALLED: list = []


def _candidate_dirs() -> list[str]:
    """按优先级列出可能的 translations 目录（源码运行 / PyInstaller 打包 / 手工拷贝）。"""
    dirs: list[str] = []

    # ① Qt 自己的答案：源码运行时是 <site-packages>/PySide6/translations；
    #    打包后 PySide6 会按包位置算，通常落在 _internal/PySide6/translations。
    try:
        from PySide6.QtCore import QLibraryInfo

        dirs.append(QLibraryInfo.path(QLibraryInfo.TranslationsPath))
    except Exception:
        pass

    # ② 打包版（PyInstaller onedir/onefile）：_MEIPASS 就是 _internal
    try:
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            dirs.append(os.path.join(mei, "PySide6", "translations"))
            dirs.append(os.path.join(mei, "translations"))
    except Exception:
        pass

    # ③ 源码运行兜底：直接按 PySide6 包目录找（QLibraryInfo 万一被裁掉也还能装）
    try:
        import PySide6

        dirs.append(os.path.join(os.path.dirname(os.path.abspath(PySide6.__file__)), "translations"))
    except Exception:
        pass

    # ④ 程序目录旁（有人把 translations 拷到 exe 同级，或运维手工补救）
    try:
        dirs.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations"))
    except Exception:
        pass

    seen = set()
    uniq: list[str] = []
    for d in dirs:
        if not d:
            continue
        key = os.path.normcase(os.path.abspath(d))
        if key in seen or not os.path.isdir(d):
            continue
        seen.add(key)
        uniq.append(d)
    return uniq


def install_qt_chinese(app=None) -> list:
    """给 app 安装简体中文翻译器，返回已装上的 QTranslator 列表（空 = 没装上）。

    幂等：同一个 app 重复调用直接返回第一次装的那批，不会叠加翻译器。
    """
    global _INSTALLED
    try:
        from PySide6.QtCore import QCoreApplication, QTranslator
    except Exception as e:  # 极端情况：PySide6 不可用（不该发生，调用点之前就 import 了）
        logger.warning("Qt 中文翻译未安装：QtCore 不可用（%s）", e)
        return []

    if app is None:
        app = QCoreApplication.instance()
    if app is None:
        # 没有 app 就没有翻译表可挂 —— 调用点写错了（必须 QApplication 之后）
        logger.warning("Qt 中文翻译未安装：当前没有 QApplication 实例")
        return []

    existing = getattr(app, "_hn_zh_translators", None)
    if existing:
        _INSTALLED = list(existing)
        return list(existing)

    installed: list = []
    directory = ""
    for directory in _candidate_dirs():
        for name in _TRANSLATION_NAMES:
            tr = QTranslator()
            try:
                if not tr.load(name, directory):
                    continue
            except Exception:
                continue
            app.installTranslator(tr)
            installed.append(tr)
            break  # 一个目录里装上一个目录就够（qtbase 优先，qt_zh_CN 只是兜底）
        if installed:
            # 第一个能加载的目录就够了；不要继续往后遍历（后面的目录是兜底，
            # 多余的同名目录再装一遍会让「后装的优先」把前面覆盖掉）。
            break

    if installed:
        # 挂在 app 上 + 模块级双保活（PySide 不持有 translator 的所有权）
        try:
            app._hn_zh_translators = installed  # type: ignore[attr-defined]
        except Exception:
            pass
        _INSTALLED = installed
        logger.info("已安装 Qt 简体中文翻译：%d 个（来自 %s）", len(installed), directory)
    else:
        logger.warning("未找到 Qt 中文翻译文件（找过：%s）", ", ".join(_candidate_dirs()) or "无可用目录")
    return installed
