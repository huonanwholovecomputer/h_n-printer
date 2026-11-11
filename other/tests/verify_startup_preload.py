# -*- coding: utf-8 -*-
"""验收脚本 · 启动时在主线程预热原生扩展（2026-11-11 打印中卡死/崩溃的修法）

现场（2026-11-11 20:57，打印机断电但 USB 连着，点「开始打印」）：
程序「卡死」—— 实际是**整个进程 access violation 死掉**，日志里没有任何 traceback。
`logs/crash_faulthandler.log` 两个线程的栈同时指向同一处竞态：

  · 打印线程：`<frozen importlib._bootstrap> create_module` ← `pdf_printer.py:1490 in _print_pdf_native`
    （那一行是函数体内的惰性 `import win32ui`，win32ui 会初始化 MFC）
  · 主线程：`Garbage-collecting` ← `enum.py` 建类 ← `gui.py:1286 in announce()`（翻纸窗改文案）

⇒ 首次加载 pywin32 原生扩展（在打印线程里）与 GC 分配（在主线程里）撞在一起。
修法不是改打印路径，而是**把加载挪到主线程、任何 QThread 起来之前**：

  `main.preload_native_modules(logger)` 在 `install_crash_handlers()` 之后、
  `QApplication(...)` / `MainWindow(...)` 之前调用一次；打印线程里那些
  `import win32print/win32ui/win32con` 就退化成查 `sys.modules` 的 no-op。

    python other/tests/verify_startup_preload.py      # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="hn_preload_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  — " + str(detail)) if detail and not cond else ""))
    if not cond:
        ok_all = False


print("\n=== ① 预热清单 ===")
import main  # noqa: E402

_want = {"win32print", "win32ui", "win32con", "pywintypes"}
check("预热清单含打印路径用到的四个 pywin32 模块",
      _want <= set(main._PRELOAD_MODULES), sorted(main._PRELOAD_MODULES))
check("清单里也含 fitz（打印/预览路径的第一处惰性 import）",
      "fitz" in main._PRELOAD_MODULES, sorted(main._PRELOAD_MODULES))

print("\n=== ② 真的把模块导进 sys.modules（不是只写了个清单） ===")
_failed = main.preload_native_modules(None)
check("preload_native_modules 返回失败清单（list）", isinstance(_failed, list), type(_failed).__name__)
for _m in main._PRELOAD_MODULES:
    if _m in _failed:
        print(f"  SKIP  {_m} 在本机导不进来（打印路径会退化，但不是本脚本能强求的）")
        continue
    check(f"{_m} 已在 sys.modules 里", _m in sys.modules)
check("再调一次是幂等 no-op（第二次应当没有新增失败）",
      main.preload_native_modules(None) == _failed, main.preload_native_modules(None))

print("\n=== ③ 调用时机：必须在 QApplication 之前、crash handler 之后 ===")
_src = open(os.path.join(LOCAL_TOOL, "main.py"), encoding="utf-8", errors="replace").read()
_body = _src.split("def main():", 1)[1]
_p = _body.find("preload_native_modules(logger)")
check("main() 里调了 preload_native_modules(logger)", _p > 0, _p)
check("在 install_crash_handlers 之后",
      _p > _body.find("install_crash_handlers(logger)"), _p)
_app_at = _body.find("QApplication(")
check("在 QApplication 之前（QThread 都还没起来）", 0 < _p < _app_at,
      f"preload={_p} QApplication={_app_at}")
check("包在 try/except 里（缺 pywin32 不该拦住启动）",
      "预热失败" in _body[_p - 400:_p + 400] or "except Exception" in _body[_p - 200:_p + 200])

print("\n=== ④ 打印路径仍保留惰性 import（兼容不装 pywin32 的场景） ===")
_pp = open(os.path.join(LOCAL_TOOL, "pdf_printer.py"), encoding="utf-8",
           errors="replace").read()
_mod_level = [ln for ln in _pp.split("\n")
              if ln[:1] not in (" ", "\t") and ln.startswith(("import win32", "from win32"))]
check("pdf_printer 模块顶层没有 import win32*（否则没装 pywin32 的机器直接起不来）",
      _mod_level == [], _mod_level[:3])
_indented = [ln for ln in _pp.split("\n")
             if ln[:1] in (" ", "\t") and ln.strip() in
             ("import win32print", "import win32ui", "import win32con")]
check("惰性 import 仍在函数体内（≥3 处）", len(_indented) >= 3, len(_indented))

print("")
if not ok_all:
    print("❌ 存在失败项")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
