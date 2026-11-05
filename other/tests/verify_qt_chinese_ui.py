# -*- coding: utf-8 -*-
"""验收 · 确认框与 Qt 标准按钮必须是中文（qtbase_zh_CN 翻译器真的装上了）。

现场问题（2026-11-04 用户）：弹窗里还会看到默认的 Yes / No / Cancel（还有 OK / Save、
输入框右键菜单的 &Copy / &Paste）。根因不是我们写的文案 —— 业务文案一直是中文 ——
而是 PySide6 **默认不加载 Qt 翻译器**，Qt 内置的那一层英文会原样画在界面上：
QMessageBox 标准按钮、QDialogButtonBox 标准按钮、QLineEdit/QTextEdit 右键菜单、
QInputDialog（补录页数）与非原生 QFileDialog。

判据（每条都能红）
  A 接线        main.py 在 `app = QApplication(...)` 之后、`from gui import MainWindow`
                之前调用 install_qt_chinese（晚装不会重译已建好的控件）
  B 真行为      子进程跑真实模块：装之前 QMessageBox / QDialogButtonBox / 右键菜单是英文，
                装之后全部中文（是/否/取消/确定/保存/复制/粘贴/全选，且没有英文残留）
  C 变异自证    用同一份探针、只是不装翻译器：after 必须仍然等于 before（英文）——
                证明 B 里的中文确实来自这次安装，而不是环境/系统语言碰巧给的
  D 幂等        同一 app 重复调用不叠加翻译器（第二次返回同一批对象），按钮仍是中文
  E 打包产物    dist 存在时，`_internal/PySide6/translations/qtbase_zh_CN.qm` 必须在
                （PyInstaller 的 Qt hook 会收；漏收则打包版又变回英文）
  F 无手写英文  不允许 `addButton("Yes"…)` / `("Cancel"…)` 这种自造标准按钮

    python tests/verify_qt_chinese_ui.py     # exit 0 = 全绿
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  ❌ {name}{(' — ' + detail) if detail else ''}")


# ═══════════════════════════════════════════════════════════════
# 子进程探针：只做「取文案」这一件事，--install 时才装翻译器
# ═══════════════════════════════════════════════════════════════
_PROBE = r'''
import json, os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
from PySide6.QtWidgets import QApplication, QMessageBox, QDialogButtonBox, QLineEdit


def snap():
    box = QMessageBox()
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel | QMessageBox.Ok)
    msg = [b.text() for b in box.buttons()]

    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Save | QDialogButtonBox.Cancel)
    dlg = [b.text() for b in bb.buttons()]

    le = QLineEdit()
    menu = [a.text() for a in le.createStandardContextMenu().actions() if a.text()]
    return {"msg": msg, "dlg": dlg, "menu": menu}


app = QApplication([])
out = {"before": snap()}
if "--install" in sys.argv:
    from i18n_zh import install_qt_chinese

    first = install_qt_chinese(app)
    second = install_qt_chinese(app)
    out["installed"] = len(first)
    out["second_len"] = len(second)
    out["second_same_objects"] = all(a is b for a, b in zip(first, second)) and len(first) == len(second)
out["after"] = snap()
print(json.dumps(out, ensure_ascii=False))
'''


def run_probe(install: bool) -> dict:
    probe = os.path.join(tempfile.mkdtemp(prefix="hn_qt_zh_"), "probe.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(_PROBE.replace("LOCAL_TOOL", repr(LOCAL_TOOL)))
    args = [sys.executable, probe] + (["--install"] if install else [])
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"探针失败 rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    if not tail:
        raise RuntimeError(f"探针没有输出 JSON：\n{proc.stdout}\n{proc.stderr}")
    return json.loads(tail[-1])


_EN_MSG = ("Yes", "No", "Cancel", "OK")
_EN_DLG = ("OK", "Save", "Cancel")
_EN_MENU = ("Copy", "Paste", "Select All")


def _has(texts: list[str], tokens) -> bool:
    return all(any(tok in t for t in texts) for tok in tokens)


print("── A 接线（main.py 的安装时机）")
_src = open(os.path.join(LOCAL_TOOL, "main.py"), encoding="utf-8").read()
_i_app = _src.find("app = QApplication(sys.argv)")
_i_install = _src.find("install_qt_chinese(app)")
_i_gui = _src.find("from gui import MainWindow")
check("main.py 调用了 install_qt_chinese(app)", _i_install > 0)
check("调用在 QApplication 之后", 0 <= _i_app < _i_install)
check("调用在建 MainWindow 之前（晚装不会重译已建控件）", 0 < _i_install < _i_gui,
      f"app={_i_app} install={_i_install} gui={_i_gui}")

_i18n = open(os.path.join(LOCAL_TOOL, "i18n_zh.py"), encoding="utf-8").read()
check("i18n_zh.py 以 qtbase_zh_CN 为主目录", '"qtbase_zh_CN"' in _i18n)
check("打包兜底路径包含 _MEIPASS/PySide6/translations", "_MEIPASS" in _i18n and '"PySide6"' in _i18n)
check("翻译器有保活（QTranslator 被 GC 会失效）", "_INSTALLED" in _i18n)

print("── B 装翻译器之后的真文案")
try:
    got = run_probe(install=True)
except Exception as e:  # noqa: BLE001
    got = {}
    check("探针可运行（--install）", False, str(e))

if got:
    before, after = got["before"], got["after"]
    check("探针启动时确实是英文（复现现场问题）", _has(before["msg"], ("Yes", "Cancel", "OK")))
    check("安装成功（至少 1 个翻译器）", got.get("installed", 0) >= 1, str(got.get("installed")))
    check("QMessageBox 标准按钮 → 是/否/取消/确定",
          all(any(x in t for t in after["msg"]) for x in ("是", "否", "取消", "确定")),
          str(after["msg"]))
    check("QDialogButtonBox 标准按钮 → 确定/保存/取消",
          all(any(x in t for t in after["dlg"]) for x in ("确定", "保存", "取消")),
          str(after["dlg"]))
    check("QLineEdit 右键菜单 → 复制/粘贴/全选",
          all(any(x in t for t in after["menu"]) for x in ("复制", "粘贴", "全选")),
          str(after["menu"]))
    _left = [t for t in (after["msg"] + after["dlg"]) if t in ("OK", "Cancel", "Save", "&Yes", "&No")]
    check("装完之后没有英文标准按钮残留", not _left, str(_left))
    _left_menu = [t for t in after["menu"] if any(tok in t for tok in _EN_MENU)]
    check("装完之后右键菜单没有英文残留", not _left_menu, str(_left_menu))

    print("── D 幂等（重复调用不叠加翻译器）")
    check("第二次调用返回同一批翻译器对象", bool(got.get("second_same_objects")),
          f"first={got.get('installed')} second={got.get('second_len')}")

print("── C 变异自证（同一探针不装翻译器 ⇒ 必须仍是英文）")
try:
    plain = run_probe(install=False)
except Exception as e:  # noqa: BLE001
    plain = {}
    check("探针可运行（不装）", False, str(e))
if plain:
    check("不装翻译器时 QMessageBox 仍是英文（中文确实来自这次安装）",
          _has(plain["after"]["msg"], _EN_MSG) and plain["after"] == plain["before"],
          str(plain["after"]["msg"]))
    check("不装翻译器时右键菜单仍是英文", _has(plain["after"]["menu"], _EN_MENU))

print("── E 打包产物（dist 存在时）")
_dist = os.path.join(LOCAL_TOOL, "dist", "HN打印工具", "_internal", "PySide6", "translations")
if os.path.isdir(os.path.join(LOCAL_TOOL, "dist", "HN打印工具")):
    check("打包版带 qtbase_zh_CN.qm（PyInstaller Qt hook 收集）",
          os.path.isfile(os.path.join(_dist, "qtbase_zh_CN.qm")), _dist)
else:
    print("  · 跳过：本机没有打包产物（dist/HN打印工具）")

print("── F 没有手写英文标准按钮")
_bad = []
for fn in sorted(os.listdir(LOCAL_TOOL)):
    if not fn.endswith(".py"):
        continue
    text = open(os.path.join(LOCAL_TOOL, fn), encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"""addButton\(\s*["'](Yes|No|Cancel|OK|Save|Close|Apply|Discard)["']""", text):
        _bad.append(f"{fn}: {m.group(0)}")
check("没有自造英文标准按钮（一律走 Qt 标准按钮 + 翻译器）", not _bad, "; ".join(_bad))

print("")
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 条 / 通过 {PASSED} 条")
    for f in FAILED:
        print(f"   - {f}")
    sys.exit(1)
print(f"✅ 全部通过（{PASSED} 条判据）")
sys.exit(0)
