# -*- coding: utf-8 -*-
"""验收 · S2-09 移动端加固（DS-22 / MB-02 / MB-04 / MB-05 / MB-06）

依据：docs/安全加固修复计划_2026-10-23.md §S2-09 与
      docs/审查报告_2026-10/data-security-2026-10-23/frag_mobile.md（MB-02/04/05/06）。

现场形状（修复前）：
  · 小程序 `console.log('登录成功, openid:', openid)` 与 `console.log('任务提交成功：', submitRes.data)`
    —— 包可反编译，openid 是跨设备稳定标识，整单快照含订单号/金额/文件名；
  · 小程序 `utils/config.js` 默认 `https://your-server.com`（可被他人注册的占位域），
    真地址只在被 gitignore 的 config.local.js 里 ⇒ clone 即构建会把 token/文档发往第三方域；
  · `capacitor.config.json` 的 `allowMixedContent: true`、无 network-security-config、无 CSP；
  · `file_paths.xml` 暴露整个外部存储根与整个 cacheDir（`path="."`），APK 下载名直接拼进路径；
  · 管理员"查看某用户订单"把 openid 拼进 API URL 与页面 URL（落进访问日志/页面栈）。

本脚本：
  A. 静态（两端 + 后端）：11 组判据（默认域名、日志、基址常量、mixed content、NSC、
     FileProvider 收窄、APK 落盘目录、CSP、URL 无 openid、X-Target-Openid 接线、
     页面交接、后端头部支持）。
  B. 行为（真 Flask 探针）：`/api/admin/user_detail` 与 `/api/orders` 的"目标用户"
     同时支持请求头与 query 回退；普通用户的目标参数被忽略；管理员/超管分支各按角色生效。
  C. 变异自证：把每一处改动改回修复前形态（客户端文本变异 + 后端源码变异子进程重放），
     对应判据必须变红。

用法：python tests/verify_mobile_hardening.py     （exit 0 = 全部通过）
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()

REPO = H.REPO
MP = os.path.join(REPO, "mobile_apps", "h_n_print")
WWW = os.path.join(REPO, "mobile_apps", "android_app", "www")
ANDROID = os.path.join(REPO, "mobile_apps", "android_app", "android", "app", "src", "main")
CAP = os.path.join(REPO, "mobile_apps", "android_app", "capacitor.config.json")

P = {
    "mp_config": os.path.join(MP, "utils", "config.js"),
    "mp_config_example": os.path.join(MP, "utils", "config.local.js.example"),
    "mp_index_js": os.path.join(MP, "pages", "index", "index.js"),
    "mp_app_js": os.path.join(MP, "app.js"),
    "mp_me_js": os.path.join(MP, "pages", "me", "me.js"),
    "mp_user_orders": os.path.join(MP, "pages", "user-orders", "user-orders.js"),
    "mp_auth_users": os.path.join(MP, "pages", "authorized-users", "authorized-users.js"),
    "app_js": os.path.join(WWW, "app.js"),
    "app_me_js": os.path.join(WWW, "me.js"),
    "app_html": os.path.join(WWW, "index.html"),
    "cap": CAP,
    "manifest": os.path.join(ANDROID, "AndroidManifest.xml"),
    "file_paths": os.path.join(ANDROID, "res", "xml", "file_paths.xml"),
    "nsc": os.path.join(ANDROID, "res", "xml", "network_security_config.xml"),
    "main_activity": os.path.join(ANDROID, "java", "cn", "hnspace", "printer", "MainActivity.java"),
    "gitignore": os.path.join(REPO, ".gitignore"),
}


def read(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def strip_c_like(text):
    """去掉 /* */、// 与 <!-- --> 注释，但**保留字符串里的内容**（URL 里的 // 不能误删）。

    判据必须吃代码不吃注释：本次改动在注释里大量引用了"修复前的写法"（例如
    `localStorage 的 hn_base_url 覆盖已删除`、`file_paths.xml 的 <cache-path ...>`），
    不剥注释就会把注释当成就地存在旧实现。
    """
    out = []
    i, n = 0, len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if text.startswith("<!--", i):
            j = text.find("-->", i + 4)
            i = n if j < 0 else j + 3
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if text.startswith("//", i):
            j = text.find("\n", i + 2)
            i = n if j < 0 else j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def walk_js(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", "build", ".git", "dist")]
        for fn in filenames:
            if fn.endswith(".js"):
                found.append(os.path.join(dirpath, fn))
    return sorted(found)


TEXT = {}
for k, p in P.items():
    TEXT[k] = read(p)
TEXT["backend"] = H.strip_comments(H.read_app_source())
TEXT["mp_all_js"] = "\n".join(strip_c_like(read(p)) for p in walk_js(MP))
TEXT["www_all_js"] = "\n".join(strip_c_like(read(p)) for p in walk_js(WWW))
# 单个机器可判断的文本（剥注释）
CODE = {k: strip_c_like(v) if k not in ("backend",) else v for k, v in TEXT.items()}
CAP_JSON = json.loads(TEXT["cap"])


def checks(t):
    """返回 {判据名: (bool, detail)}。t 的值是"已剥注释的代码文本"（backend 已剥 # 注释）。"""
    c = {}
    mp_cfg = t["mp_config"]
    c["A1 小程序默认基址=生产 https 域名（仓库内无占位域）"] = (
        "PRODUCTION_BASE_URL = 'https://hn-space.cn'" in mp_cfg
        and re.search(r"let _base_url = PRODUCTION_BASE_URL", mp_cfg) is not None
        and "your-server.com" not in mp_cfg + t["mp_config_example"],
        "默认值不在 config.js（或仍含占位域）")

    gi = t["gitignore"]
    example_rel = "mobile_apps/h_n_print/utils/config.local.js"
    c["A2 config.local.js.example 入库且未被同名 gitignore 规则排除"] = (
        os.path.isfile(P["mp_config_example"])
        and example_rel in gi
        and (example_rel + ".example") not in gi,
        "模板文件缺失或被 gitignore 误伤")

    bad_logs = [ln.strip() for ln in t["mp_index_js"].splitlines()
                if "console." in ln and ("openid" in ln or "submitRes.data" in ln)]
    c["A3 小程序日志不打印 openid / 提交快照"] = (
        not bad_logs,
        "仍打印 openid 或整单快照: %r" % (bad_logs[:2],))

    app_js = t["app_js"]
    c["A4 APP 基址为编译期常量（无 localStorage 覆盖，只留显式测试钩子）"] = (
        "localStorage.getItem('hn_base_url')" not in app_js
        and "hn_base_url" not in app_js
        and "__HN_TEST_BASE_URL__" in app_js
        and "const BASE_URL = TEST_BASE_URL || DEFAULT_BASE_URL;" in app_js,
        "仍可从 localStorage 改写基址")

    c["A5 allowMixedContent 已关闭"] = (
        CAP_JSON.get("android", {}).get("allowMixedContent") is False,
        "capacitor.config.json 仍允许混合内容")

    nsc = t["nsc"]
    c["A6 显式 NSC 禁明文且 manifest 引用"] = (
        'cleartextTrafficPermitted="false"' in nsc
        and 'android:networkSecurityConfig="@xml/network_security_config"' in t["manifest"],
        "NSC 缺失或未被 manifest 引用")

    fp = t["file_paths"]
    c["A7 FileProvider 只暴露 cacheDir/updates/（无 external-path、无 path=\".\"）"] = (
        "external-path" not in fp
        and 'path="."' not in fp
        and '<cache-path name="updates" path="updates/"' in fp,
        "FileProvider 路径仍过宽")

    ma = t["main_activity"]
    c["A8 APK 固定落 getCacheDir()/updates/ 且文件名收窄"] = (
        'new File(getCacheDir(), "updates")' in ma
        and "sanitizeApkName(fileName)" in ma
        and "new File(getCacheDir(), fileName)" not in ma
        and re.search(r"String sanitizeApkName\(String raw\)", ma) is not None,
        "APK 仍按桥入参直接拼路径")

    html = t["app_html"]
    c["A9 index.html 有 CSP（禁止对象/内嵌帧，connect-src 限生产域）"] = (
        "Content-Security-Policy" in html
        and "default-src 'self'" in html
        and "object-src 'none'" in html
        and "base-uri 'none'" in html
        and "connect-src 'self' https://hn-space.cn" in html,
        "CSP 缺失或未限定 connect-src")

    fs_leaks = []
    for label, blob in (("小程序", t["mp_all_js"]), ("APP", t["www_all_js"])):
        for m in re.finditer(r"[?&]openid=", blob):
            fs_leaks.append("%s@%d" % (label, m.start()))
    c["A10 客户端请求 URL 不再携带 openid（改走 X-Target-Openid 头）"] = (
        not fs_leaks,
        "仍在 URL 里拼 openid: %r" % (fs_leaks[:4],))

    mp_uo = t["mp_user_orders"]
    c["A11 目标用户走 X-Target-Openid 头（两端 6 处）"] = (
        mp_uo.count("'X-Target-Openid': openid") >= 2
        and "'X-Target-Openid': openid" in t["mp_auth_users"]
        and t["app_me_js"].count("X-Target-Openid") >= 3,
        "头接线不足（小程序 user-orders/authorized-users 或 APP me.js）")

    c["A12 页面跳转不再带 openid，改用 app.globalData 交接"] = (
        "user-orders?openid=" not in t["mp_me_js"] + t["mp_auth_users"]
        and "userOrdersTarget" in t["mp_app_js"]
        and "userOrdersTarget" in t["mp_me_js"]
        and "userOrdersTarget" in t["mp_auth_users"]
        and "userOrdersTarget" in mp_uo,
        "页面 URL 仍带 openid 或未接线 globalData 交接")

    be = t["backend"]
    c["A13 后端两个路由读 X-Target-Openid（query 回退）"] = (
        be.count("def _target_openid_arg()") == 1
        and be.count('request.headers.get("X-Target-Openid"') >= 1
        and be.count("_target_openid_arg()") >= 2,
        "后端未接头部或未覆盖两个路由")
    return c


def report(results, prefix=""):
    for name, (ok, detail) in results.items():
        H.chk(prefix + name, ok, detail)


print("=" * 64)
print("A. 静态判据（S2-09 移动端加固）")
print("=" * 64)
base = checks(CODE)
report(base)

# ---------------------------------------------------------------- B. 行为（真 Flask）
print("\n" + "=" * 64)
print("B. 行为（真 Flask + 真 SQLite：目标用户头/query 两条路）")
print("=" * 64)

TMP, DST = H.make_copy("hn_s209_")
probe = H.run_probe(DST, r"""
import json
def mk(openid):
    return {"Authorization": "Bearer " + backend.token_serializer.dumps(openid)}
backend.is_admin = lambda o: o in ("p-admin", "p-super")
backend.SUPER_ADMIN_OPENID = "p-super"
u = backend.get_user_db()
u.execute("INSERT OR REPLACE INTO users (openid, nickname, avatar_path, updated_at, role)"
          " VALUES (?,?,?,?,?)", ("p-user", "u", "", "2026-01-01 00:00:00", "user"))
u.commit(); u.close()
A = mk("p-admin"); U = mk("p-user"); S = mk("p-super")
def with_h(h, target):
    h2 = dict(h); h2["X-Target-Openid"] = target; return h2
out = {}
out["detail_none"] = http.get("/api/admin/user_detail", headers=A).status_code
out["detail_header"] = http.get("/api/admin/user_detail", headers=with_h(A, "p-victim")).status_code
out["detail_query"] = http.get("/api/admin/user_detail?openid=p-victim", headers=A).status_code
out["orders_user"] = http.get("/api/orders", headers=U).status_code
out["orders_user_target"] = http.get("/api/orders", headers=with_h(U, "p-victim")).status_code
out["orders_admin_target"] = http.get("/api/orders", headers=with_h(A, "p-victim")).status_code
out["orders_admin_query"] = http.get("/api/orders?openid=p-victim", headers=A).status_code
out["orders_super_target"] = http.get("/api/orders", headers=with_h(S, "p-victim")).status_code
print("RESULT=" + json.dumps(out))
""")
behave = {}
try:
    behave = json.loads(H.probe_value(probe, "RESULT") or "{}")
except Exception:
    behave = {}
H.chk("B0 后端探针能起（init_db + test_client）", bool(behave),
      (probe.stderr or "")[-500:] or "无 RESULT")
H.chk("B1 /api/admin/user_detail 无目标 ⇒ 400（缺少 openid）", behave.get("detail_none") == 400,
      behave.get("detail_none"))
H.chk("B2 带头目标 ⇒ 403（头被解析，走到「无权查看该用户」）", behave.get("detail_header") == 403,
      behave.get("detail_header"))
H.chk("B3 带 query 目标 ⇒ 403（老客户端向后兼容）", behave.get("detail_query") == 403,
      behave.get("detail_query"))
H.chk("B4 /api/orders 普通用户带目标头 ⇒ 仍 200（目标参数对普通用户不生效）",
      behave.get("orders_user") == 200 and behave.get("orders_user_target") == 200,
      (behave.get("orders_user"), behave.get("orders_user_target")))
H.chk("B5 /api/orders 管理员带目标头 ⇒ 403（未授权该用户，头部被采纳）",
      behave.get("orders_admin_target") == 403, behave.get("orders_admin_target"))
H.chk("B6 /api/orders 管理员带 ?openid= ⇒ 同样 403（两路口径一致）",
      behave.get("orders_admin_query") == 403, behave.get("orders_admin_query"))
H.chk("B7 /api/orders 超管带目标头 ⇒ 200（指定用户分支）",
      behave.get("orders_super_target") == 200, behave.get("orders_super_target"))

# ---------------------------------------------------------------- C. 变异自证（客户端文本）
print("\n" + "=" * 64)
print("C. 变异自证：改回修复前形态，对应判据必须变红")
print("=" * 64)


def mutate(key, old, new, expect):
    """对 CODE 副本做一处替换，断言只有 expect 判据变红。"""
    mt = dict(CODE)
    if old not in mt[key]:
        H.chk("★ 变异锚点命中：" + expect, False, "锚点未命中: %r" % old[:80])
        return
    mt[key] = mt[key].replace(old, new, 1)
    res = checks(mt)
    ok_before = base[expect][0]
    now = res[expect][0]
    others = [n for n, (v, _) in res.items() if v is False and n != expect]
    H.chk("★ 变异后变红：" + expect, ok_before and (not now) and not others,
          "before=%s now=%s 其它红=%r" % (ok_before, now, others[:3]))


mutate("mp_config",
       "const PRODUCTION_BASE_URL = 'https://hn-space.cn'",
       "const PRODUCTION_BASE_URL = 'https://your-server.com'",
       "A1 小程序默认基址=生产 https 域名（仓库内无占位域）")
mutate("mp_index_js",
       "console.log('[doLogin] 登录成功')",
       "console.log('登录成功, openid:', openid)",
       "A3 小程序日志不打印 openid / 提交快照")
mutate("app_js",
       "const TEST_BASE_URL = (typeof window !== 'undefined' && window.__HN_TEST_BASE_URL__) || '';",
       "const TEST_BASE_URL = localStorage.getItem('hn_base_url') || '';",
       "A4 APP 基址为编译期常量（无 localStorage 覆盖，只留显式测试钩子）")
mutate("app_html",
       "default-src 'self'",
       "default-src *",
       "A9 index.html 有 CSP（禁止对象/内嵌帧，connect-src 限生产域）")
mutate("mp_auth_users",
       "header: { 'Authorization': 'Bearer ' + token, 'X-Target-Openid': openid },",
       "header: { 'Authorization': 'Bearer ' + token },\n        data: { openid },",
       "A11 目标用户走 X-Target-Openid 头（两端 6 处）")
mutate("mp_app_js",
       "    userOrdersTarget: null,",
       "",
       "A12 页面跳转不再带 openid，改用 app.globalData 交接")

# 配置文件类变异（JSON / XML / Java）
cap_mut = json.loads(TEXT["cap"])
cap_mut.setdefault("android", {})["allowMixedContent"] = True
mt = dict(CODE)
mt["cap"] = TEXT["cap"]  # checks 读取的是模块级 CAP_JSON，故用临时替换验证：见下
_orig_cap = CAP_JSON
CAP_JSON["android"]["allowMixedContent"] = True
res_cap = checks(CODE)
H.chk("★ 变异后变红：A5 allowMixedContent 已关闭", base["A5 allowMixedContent 已关闭"][0]
      and not res_cap["A5 allowMixedContent 已关闭"][0],
      "allowMixedContent=true 时判据仍绿")
CAP_JSON["android"]["allowMixedContent"] = _orig_cap["android"]["allowMixedContent"]

mt = dict(CODE)
mt["manifest"] = mt["manifest"].replace('android:networkSecurityConfig="@xml/network_security_config"\n        ', "")
res = checks(mt)
H.chk("★ 变异后变红：A6 显式 NSC 禁明文且 manifest 引用",
      base["A6 显式 NSC 禁明文且 manifest 引用"][0]
      and not res["A6 显式 NSC 禁明文且 manifest 引用"][0], "去掉 manifest 引用仍绿")

mt = dict(CODE)
mt["file_paths"] = ('<paths xmlns:android="http://schemas.android.com/apk/res/android">\n'
                    '    <external-path name="my_images" path="." />\n'
                    '</paths>')
res = checks(mt)
H.chk("★ 变异后变红：A7 FileProvider 只暴露 cacheDir/updates/（无 external-path、无 path=\".\"）",
      base["A7 FileProvider 只暴露 cacheDir/updates/（无 external-path、无 path=\".\"）"][0]
      and not res["A7 FileProvider 只暴露 cacheDir/updates/（无 external-path、无 path=\".\"）"][0],
      "恢复 external-path 仍绿")

mt = dict(CODE)
mt["main_activity"] = mt["main_activity"].replace(
    'apk = new File(updateDir, sanitizeApkName(fileName));',
    'apk = new File(getCacheDir(), fileName);')
res = checks(mt)
H.chk("★ 变异后变红：A8 APK 固定落 getCacheDir()/updates/ 且文件名收窄",
      base["A8 APK 固定落 getCacheDir()/updates/ 且文件名收窄"][0]
      and not res["A8 APK 固定落 getCacheDir()/updates/ 且文件名收窄"][0], "改回旧写法仍绿")

# ---------------------------------------------------------------- C2. 后端变异（子进程）
print("\n---- C2. 后端变异：让 _target_openid_arg 忽略请求头 ----")
try:
    _mut_tmp, MUT = H.mutated_copy([
        ('    val = (request.headers.get("X-Target-Openid", "") or "").strip()\n'
         '    if not val:\n',
         '    val = ""\n'
         '    if not val:\n'),
    ], prefix="hn_s209_mut_")
    mprobe = H.run_probe(MUT, r"""
import json
def mk(openid):
    return {"Authorization": "Bearer " + backend.token_serializer.dumps(openid)}
backend.is_admin = lambda o: o in ("p-admin", "p-super")
backend.SUPER_ADMIN_OPENID = "p-super"
A = mk("p-admin")
h = dict(A); h["X-Target-Openid"] = "p-victim"
out = {}
out["detail_header"] = http.get("/api/admin/user_detail", headers=h).status_code
out["orders_admin_target"] = http.get("/api/orders", headers=h).status_code
print("RESULT=" + json.dumps(out))
""")
    mval = json.loads(H.probe_value(mprobe, "RESULT") or "{}")
    H.chk("★ 变异后变红：忽略 X-Target-Openid ⇒ detail_header 回 400 / orders 回 200",
          mval.get("detail_header") == 400 and mval.get("orders_admin_target") == 200,
          mval)
except Exception as e:  # noqa: BLE001
    H.chk("★ 变异后变红：后端忽略请求头（变异副本构建失败）", False, repr(e))

H.finish()
