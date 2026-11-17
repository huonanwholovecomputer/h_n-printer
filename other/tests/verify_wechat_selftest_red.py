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


JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       ".wechat_selftest_journal.json")


def _restore_journal():
    """上次运行被硬杀（run_all 超时 = TerminateProcess，finally 根本不会执行）时，
    源码会停留在"改坏"的样子，而没有任何脚本会告诉你。这里按落盘日志的原始字节写回去。"""
    if not os.path.exists(JOURNAL):
        return False
    import base64
    import json
    with open(JOURNAL, "r", encoding="utf-8") as f:
        saved = json.load(f)
    for path, b64 in saved.items():
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"[RESTORE] 上次被中断，已按原始字节还原：{path}")
    os.remove(JOURNAL)
    return True


def _save_journal(saved):
    import base64
    import json
    tmp = JOURNAL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({p: base64.b64encode(b).decode() for p, b in saved.items()}, f)
    os.replace(tmp, JOURNAL)


def main():
    # 这个脚本会**临时改坏源码再还原**（为了证明验收判据能真红）。
    # 全量回归（tests/run_all.py）里默认跳过，避免与其它脚本交叉；要跑就显式给环境变量：
    #   WECHAT_SELFTEST=1 python tests/verify_wechat_selftest_red.py
    if _restore_journal():
        print("[RESTORE] 先恢复了上次被中断的改动，继续。")
    if os.environ.get("WECHAT_SELFTEST") != "1":
        print("[HN-SKIP] 真红自证默认不跑（会临时改坏小程序源码再还原）："
              "这一轮「判据能不能真红」没有任何证据；设 WECHAT_SELFTEST=1 显式运行")
        return 0

    ok = True
    for name, fpath, old, new, script in CASES:
        # 按**字节**读写：文本模式读会把 CRLF 折叠成 LF，还原时就把整个文件的行尾改掉
        # （git 会显示成整文件改动），旧版就是这么静默污染源码的。
        with open(fpath, "rb") as f:
            raw = f.read()
        crlf = b"\r\n" in raw
        norm = raw.decode("utf-8").replace("\r\n", "\n")
        if norm.count(old) != 1:
            print(f"[SETUP-FAIL] {name}: 原文片段出现 {norm.count(old)} 次（应为 1），跳过")
            ok = False
            continue
        mutated = norm.replace(old, new, 1)
        if crlf:
            mutated = mutated.replace("\n", "\r\n")
        _save_journal({fpath: raw})
        try:
            with open(fpath, "wb") as f:
                f.write(mutated.encode("utf-8"))
            code, fails = run(script)
            red = code == 1 and len(fails) > 0
            print(f"[{'RED-OK' if red else 'RED-FAIL'}] {name}  -> 脚本 exit={code}, 失败 {len(fails)} 项")
            for ln in fails[:4]:
                print("          " + ln)
            ok = ok and red
        finally:
            with open(fpath, "wb") as f:
                f.write(raw)
            if os.path.exists(JOURNAL):
                os.remove(JOURNAL)
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
