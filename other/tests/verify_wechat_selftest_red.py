# -*- coding: utf-8 -*-
"""四个需求的验收判据「真红」自证：逐条故意改坏 → 跑对应脚本（必须 exit 1）→ 还原 → 再跑（必须 exit 0）。
不提交、不动他人文件；只在 mobile_apps/h_n_print 下临时改一行再还原。"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = r"D:\打印机项目"
MP = os.path.join(REPO, "mobile_apps", "h_n_print")

CASES = [
    # (名称, 文件, 原文片段, 改坏片段, 验收脚本)
    (
        "①近N条订单：去掉管理员门控（非管理员也发请求）",
        os.path.join(MP, "pages", "me", "me.js"),
        "if (this.data.userRole !== 'admin' && wx.getStorageSync('userRole') !== 'admin') return",
        "if (false) return",
        os.path.join(REPO, "other", "tests", "verify_wechat_recent_orders.js"),
    ),
    (
        "①近N条订单：N 编辑器写接口从 POST 改成 GET",
        os.path.join(MP, "pages", "me", "me.js"),
        "url: CONFIG.BASE_URL + '/api/admin/recent_orders_limit',\n        method: 'POST',",
        "url: CONFIG.BASE_URL + '/api/admin/recent_orders_limit',\n        method: 'GET',",
        os.path.join(REPO, "other", "tests", "verify_wechat_recent_orders.js"),
    ),
    (
        "②离线攒批：恢复「设备离线就直接 return」的旧拦截（2026-11 已放开，随时可切）",
        os.path.join(MP, "pages", "index", "index.js"),
        "      const index = e.currentTarget.dataset.index\n      const device = (this.data.claimingDevices || [])[index]",
        "      if (((this.data.claimingDevices || [])[e.currentTarget.dataset.index] || {}).online === false) return\n      const index = e.currentTarget.dataset.index\n      const device = (this.data.claimingDevices || [])[index]",
        os.path.join(REPO, "other", "tests", "verify_wechat_offline_stack.js"),
    ),
    (
        "③滚轮：拖动中把 timeWheelValue 写回（复现「慢滑被拽回起点」）",
        os.path.join(MP, "pages", "index", "index.js"),
        "      this._wheelHi = hi\n      this._wheelMi = mi\n      // 只更新派生索引",
        "      this._wheelHi = hi\n      this._wheelMi = mi\n      this.setData({ timeWheelValue: [hi, mi] })\n      // 只更新派生索引",
        os.path.join(REPO, "other", "tests", "verify_wechat_wheel.js"),
    ),
    (
        "④嵌套滚动：把「内层到边后的余量交给外层」去掉（内层吃完就吃掉整帧）",
        os.path.join(MP, "utils", "scroll.wxs"),
        "      var rest = need - take;",
        "      var rest = 0;",
        os.path.join(REPO, "other", "tests", "verify_wechat_nested_scroll.js"),
    ),
    (
        "④嵌套滚动：外层贴边判定的方向反过来（上滑却去比 minY）",
        os.path.join(MP, "utils", "scroll.wxs"),
        "  return state.y <= state.minY + 0.5;",
        "  return state.maxY > 0 && state.y >= state.maxY - 0.5;",
        os.path.join(REPO, "other", "tests", "verify_nested_scroll_handoff.js"),
    ),
]


def run(script):
    p = subprocess.run([os.environ.get("NODE_BIN", "node"), script],
                       cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
    fails = [ln for ln in (p.stdout or "").splitlines() if ln.startswith("[FAIL]")]
    return p.returncode, fails


def main():
    # 这个脚本会**临时改坏源码再还原**（为了证明验收判据能真红）。
    # 全量回归（tests/run_all.py）里默认跳过，避免与其它脚本交叉；要跑就显式给环境变量：
    #   WECHAT_SELFTEST=1 python tests/verify_wechat_selftest_red.py
    if os.environ.get("WECHAT_SELFTEST") != "1":
        print("[SKIP] 真红自证会临时改坏源码再还原；设 WECHAT_SELFTEST=1 显式运行。")
        return 0

    ok = True
    for name, fpath, old, new, script in CASES:
        with open(fpath, "r", encoding="utf-8") as f:
            src = f.read()
        if src.count(old) != 1:
            print(f"[SETUP-FAIL] {name}: 原文片段出现 {src.count(old)} 次（应为 1），跳过")
            ok = False
            continue
        fd, bak = tempfile.mkstemp(suffix=".bak")
        os.close(fd)
        shutil.copyfile(fpath, bak)
        try:
            with open(fpath, "w", encoding="utf-8", newline="") as f:
                f.write(src.replace(old, new, 1))
            code, fails = run(script)
            red = code == 1 and len(fails) > 0
            print(f"[{'RED-OK' if red else 'RED-FAIL'}] {name}  -> 脚本 exit={code}, 失败 {len(fails)} 项")
            for ln in fails[:4]:
                print("          " + ln)
            ok = ok and red
        finally:
            shutil.copyfile(bak, fpath)
            os.remove(bak)
        # 还原后必须重新变绿
        code, fails = run(script)
        green = code == 0
        print(f"[{'RESTORE-OK' if green else 'RESTORE-FAIL'}] 还原后 {os.path.basename(script)} exit={code}")
        ok = ok and green
        print()

    print("=" * 60)
    print("真红自证：" + ("全部通过（每条都能真红、还原后都能真绿）" if ok else "存在失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
