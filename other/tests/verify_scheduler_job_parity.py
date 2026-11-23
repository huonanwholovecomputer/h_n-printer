#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 · 定时任务注册清单：直跑（app.py __main__）与生产（gunicorn_config）必须一致

2026-11-23 现场：生产用 gunicorn（systemd printer-backend），定时任务全在
`gunicorn_config.post_worker_init()` 里注册；而 `app.py` 的 `__main__` 直跑分支另有一份清单。
两份清单长期漂移 —— 直跑注册 14 个、gunicorn 只注册 9 个，于是生产上
purge_test_devices / resync_prices / release_stuck_frozen / expire_stale_scheduled /
rotate_dl_codes **从未跑过**（预约单不超时取消、改价失败不补算、冻结单不自愈、下载码不轮换、
测试设备越积越多）。本脚本把"两份清单必须相等"钉成判据，防再漂移。

只做静态解析（AST），不起服务、不连数据库。

用法：python other/tests/verify_scheduler_job_parity.py   （exit 0 = 全绿）
"""
import ast
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
APP_PY = os.path.join(BACKEND, "app.py")
GUNICORN = os.path.join(BACKEND, "gunicorn_config.py")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def _add_job_ids(tree):
    """收集 add_job(..., id="xxx") 的 id，以及 scheduler 任务函数名。"""
    ids, fns = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "add_job":
            for kw in node.keywords:
                if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                    ids.append(kw.value.value)
            if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id != "fn":
                fns.append(node.args[0].id)
    return ids, fns


def main():
    app_src = open(APP_PY, "r", encoding="utf-8").read()
    gunicorn_src = open(GUNICORN, "r", encoding="utf-8").read()
    app_tree = ast.parse(app_src)
    gun_tree = ast.parse(gunicorn_src)

    # ① 直跑分支的 add_job 清单
    main_node = next((n for n in app_tree.body
                      if isinstance(n, ast.If)
                      and "__main__" in ast.dump(n.test)), None)
    check("app.py 有 __main__ 直跑分支", main_node is not None)
    dev_ids, dev_fns = _add_job_ids(main_node) if main_node is not None else ([], [])
    check("直跑分支注册了定时任务", len(dev_ids) >= 10, f"count={len(dev_ids)}")
    check("直跑分支 add_job 都带 id", len(dev_ids) == len(set(dev_ids)),
          f"ids={sorted(dev_ids)}")

    # ② gunicorn_config 里注册的 id（_ensure_job("id", ...) 或旧式 scheduler.add_job(..., id=)）
    gun_ids, gun_fns = _add_job_ids(gun_tree)
    gun_ensure = re.findall(r'_ensure_job\(\s*"([^"]+)"', gunicorn_src)
    prod_ids = sorted(set(gun_ids) | set(gun_ensure))
    check("gunicorn_config 有 post_worker_init", "def post_worker_init(" in gunicorn_src)
    check("gunicorn_config 注册了定时任务", len(prod_ids) >= 10, f"count={len(prod_ids)}")
    check("生产清单无重复 id", len(gun_ensure) == len(set(gun_ensure)),
          f"ids={sorted(gun_ensure)}")

    # ③ 两份清单必须完全一致（这才是真判据）
    missing = sorted(set(dev_ids) - set(prod_ids))
    extra = sorted(set(prod_ids) - set(dev_ids))
    check("生产 gunicorn 覆盖直跑分支的全部定时任务",
          not missing, "生产缺: " + ", ".join(missing))
    check("生产 gunicorn 没有直跑分支之外的任务", not extra,
          "多出: " + ", ".join(extra))

    # ④ 生产注册的任务函数必须真的存在于 app.py（防笔误 import 崩 worker）
    for fn in sorted(set(gun_fns)):
        check(f"app.py 存在被注册的函数 {fn}()", f"def {fn}(" in app_src)

    ok = all(RESULTS)
    print("\n" + "=" * 68)
    print(f"定时任务注册清单一致性：{sum(RESULTS)}/{len(RESULTS)} 条通过" + (" ✅" if ok else " ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
