# -*- coding: utf-8 -*-
"""验收 · 🟡-D4 nginx 不得直出上传目录（`location /uploads/`）

背景（审查报告 D4 / findings_D1_backend_a）：两份 nginx 配置都有一条

    location /uploads/ {
        alias /opt/printer-backend/uploads/;
        expires 1h;
    }

`/opt/printer-backend/uploads/` 里装的是**顾客上传的原始文档**，**且与它们混放**的还有
几个固定文件名的服务端元数据：`md5_index.json`（md5 → 原始文件名 + 相对路径 + 页数）、
`security_config.json`、`retention_config.json`、`order_counter.json`、`avatars/`。
nginx `alias` 是**完全不鉴权**的静态下发 —— 只要照 `DEPLOY.md` 重新部署一次，
`GET /uploads/md5_index.json` 就是一个确定可用的**任意文件读取入口**（文件名固定，
无需爆破），顺带泄露全体顾客的文件名与结构。

线上当前没暴露，只是因为服务器上的配置还是旧版（实测 `/uploads/md5_index.json` → 404）；
也就是说这是"**照着文档做一遍就打开**"的地雷，而不是"已经响了"的洞。

本脚本钉住三件事：
  A. 两份 conf 里都**不得存在外部可直接访问的 /uploads/ location** —— 判据是
     "该 location 既没有 `internal;` 也没有 `deny all;`"（删掉整块 / 加 internal 两种
     修法都能通过；粗暴地"一见 /uploads/ 就红"会把合规写法误杀）；
  B. **没有任何代码依赖 `/uploads/…` 这个 URL**（后端 + 三个前端 + static）——
     这是"删掉它不会破坏功能"的证据（下发一律走 Flask 路由 `/api/file/<id>`、
     `/api/avatar?openid=`，见 B 节的形态断言）；
  C. `DEPLOY.md` 必须留下"勿直出上传目录"的警示（否则下一个人照着文档又加回来）。

三条检查**都自带对拍**：把直出块塞回去 / 把代码里塞一个 `/uploads/` URL，
检查器必须报错；而"塞回去但加 `internal;`"必须**不**报错（证明判据是语义而非字面）。

用法：python tests/verify_nginx_uploads.py     （exit 0 = 全部通过）
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
CONFS = [
    os.path.join(BACKEND, "nginx-http.conf"),
    os.path.join(BACKEND, "nginx-https.conf"),
]
DEPLOY = os.path.join(BACKEND, "DEPLOY.md")
APP_PY = os.path.join(BACKEND, "app.py")

# 要扫"有没有人用 /uploads/ 这个 URL"的目录/文件（node_modules / dist 是构建产物，跳过）
FRONTEND_DIRS = [
    os.path.join(REPO, "mobile_apps", "h_n_print"),
    os.path.join(REPO, "mobile_apps", "android_app", "www"),
    os.path.join(BACKEND, "static"),
]
SKIP_DIR_NAMES = {"node_modules", "dist", "__pycache__", ".git",
                  "build", "updata", "updates", "tools"}
TEXT_SUFFIXES = {".js", ".mjs", ".ts", ".json", ".wxml", ".wxss", ".html",
                 ".css", ".py", ".md", ".conf"}

# 解析器的"存活下限"（每份 conf 至少该解析出这么多 location）。按文件给：
# 删掉 uploads 那一块后 http 是 4 个、https 是 6 个，用统一阈值会把**合法的删减**
# 误判成"解析器失效"（第一版就因为写死 >= 5 而假红一次）。
MIN_LOCATIONS = {"nginx-http.conf": 4, "nginx-https.conf": 6}

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ============================================================
# 解析 helpers
# ============================================================

def strip_comments(src):
    """把 nginx `#` 行注释**置空**（逐行把 `#` 起的内容换成等长空格）。

    两个要点：
      · 必须剥注释 —— 注释里完全可能出现 `location /uploads/ { … }` 这种"引用原文"的
        字样，不剥就会被当成真的 location（我写解释性注释时当场踩了这个雷）；
      · 必须**等长替换**而不是删掉 —— 后面的 `span` 用来从**原始文本**里摘块，
        一旦字符偏移变了，摘下来的就是错位的内容。
    """
    out = []
    for line in src.splitlines(keepends=True):
        i = line.find("#")
        if i < 0:
            out.append(line)
            continue
        tail = "\n" if line.endswith("\n") else ""
        body = line[:i]
        out.append(body + " " * (len(line) - len(body) - len(tail)) + tail)
    return "".join(out)


def _norm_uri(token):
    """把 location 的匹配式归一化为纯路径。

    nginx 的 location 有五种写法：`location /p/`、`location = /p`、`location ^~ /p/`、
    `location ~ re`、`location ~* re`。取最后一个空白分隔 token 后还要剥掉正则锚点
    （`~* ^/uploads/` 的 token 是 `^/uploads/`）——**否则正则式直出会被漏检**，
    这正是本脚本第一版对拍当场抓到的问题。
    """
    u = token.strip()
    for pfx in ("~*", "~", "=", "^~"):
        if u.startswith(pfx):
            u = u[len(pfx):].strip()
    return u.lstrip("^").rstrip("$")


def location_blocks(src):
    """产出 conf 里的 (uri, body, lineno, span)。用花括号配平取 body
    （这几份 conf 的 location 内部不再嵌 location，不做递归）。
    span = (起, 止) 的字符下标（含行尾换行），供"把块摘掉"用。

    解析在**注释置空后**的文本上进行（`strip_comments` 等长替换，故 lineno/span
    对原始文本同样成立）。
    """
    out = []
    masked = strip_comments(src)
    for m in re.finditer(r"\blocation\s+([^{]*?)\s*\{", masked):
        spec = m.group(1).strip().split()
        uri = _norm_uri(spec[-1]) if spec else ""
        i = m.end() - 1                      # 指向 '{'
        depth, j = 0, i
        while j < len(masked):
            if masked[j] == "{":
                depth += 1
            elif masked[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        nl = src.find("\n", j)
        end = (nl + 1) if nl != -1 else (j + 1)
        out.append((uri, src[i + 1:j], src[:m.start()].count("\n") + 1, (m.start(), end)))
    return out


def _is_upload_uri(uri):
    return uri == "/uploads" or uri.startswith("/uploads/")


_SAFE_BODY = re.compile(r"\b(internal|deny\s+all)\b")


def exposed_upload_locations(src):
    """外部可直接访问的 /uploads/ location（含行号）。空 = 已封堵。"""
    return [(uri, line) for uri, body, line, _sp in location_blocks(src)
            if _is_upload_uri(uri) and not _SAFE_BODY.search(body)]


def remove_upload_locations(src):
    """摘掉所有 /uploads/ location（对拍用：构造一个不含该块的基线文本）。"""
    spans = [sp for uri, _b, _l, sp in location_blocks(src) if _is_upload_uri(uri)]
    for start, end in reversed(spans):
        src = src[:start] + src[end:]
    return src


_UPLOAD_URL = re.compile(r"/uploads(?![A-Za-z0-9_])")


def upload_url_references(text):
    """代码里把 `/uploads…` 当成 URL 用的行号。

    判据故意取"**字面出现 `/uploads`（后不接字母/数字/下划线）**"这一最严口径：
    本仓库合法的写法只有 `UPLOAD_DIR = "uploads"` 与 `os.path.join(UPLOAD_DIR, …)`，
    **都不含前导斜杠**；`/api/upload` 的路径段是 `/upload` 不含 s，也不会命中。
    所以任何一次命中都值得人看一眼 —— 连带 `https://host/uploads/…` 这种完整 URL 也抓。
    """
    return [(i, line.strip()[:90])
            for i, line in enumerate(text.splitlines(), 1)
            if _UPLOAD_URL.search(line)]


def iter_frontend_files():
    for root_path in FRONTEND_DIRS:
        if os.path.isfile(root_path):
            yield root_path
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in TEXT_SUFFIXES:
                    yield os.path.join(dirpath, fn)


def scan_upload_url_refs():
    """返回 [(相对路径, 行号, 行内容)] —— 全部对 /uploads 的引用。"""
    hits = []
    for path in [APP_PY] + sorted(iter_frontend_files()):
        rel = os.path.relpath(path, REPO)
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError as e:
            hits.append((rel, 0, f"<读取失败: {e}>"))
            continue
        for line_no, snippet in upload_url_references(text):
            hits.append((rel, line_no, snippet))
    return hits


def deploy_warns(text):
    """DEPLOY.md 里有没有"勿直出上传目录"的警示（要求同一段里既提上传目录又提直出）。"""
    for para in re.split(r"\n\s*\n", text):
        if "直出" not in para:
            continue
        if re.search(r"(勿|不要|禁止|不得|绝不|避免)\s*直出", para) and \
           re.search(r"uploads|上传目录", para):
            return True
    return False


# ============================================================
# A. 两份 conf 都不得直出 uploads
# ============================================================
print("=== A. nginx 不得直出上传目录（/uploads/ location 无 internal / deny）===")
confs = {}
for path in CONFS:
    name = os.path.basename(path)
    src = open(path, encoding="utf-8").read()
    confs[path] = src
    blocks = location_blocks(src)
    uris = [u for u, _b, _l, _sp in blocks]
    print(f"     {name}: {len(blocks)} 个 location -> {', '.join(uris)}")

    check(f"{name} 解析到全部 location（解析器没失效）",
          len(blocks) >= MIN_LOCATIONS[name],
          f"只解析到 {len(blocks)} 个: {uris}")

    bad = exposed_upload_locations(src)
    check(f"★ {name} 无「外部可直接访问的 /uploads/ location」", not bad,
          "；".join(f"{u} @第{ln}行" for u, ln in bad))

    # 删干净：剥掉注释后 alias 路径不应再出现（防"块还在但换个写法"）
    check(f"{name} 剥注释后不再出现 alias 上传目录路径",
          "/opt/printer-backend/uploads" not in strip_comments(src))

    # 注释要留下"为什么不给"，否则下一个人会"顺手加回去"
    check(f"{name} 留有说明性注记（点明不给 nginx 直出）",
          re.search(r"直出|任意文件(读取|下载)|不给\s*nginx", src) is not None)

    # 别把该留的删了 —— 关键 location 与限速/代理参数必须在
    for must_uri in ("/api/upload", "/socket.io/", "/api/", "/"):
        check(f"{name} 仍保留 location {must_uri}",
              any(u == must_uri for u in uris), f"实际: {uris}")
    check(f"{name} 仍保留上传体积上限 client_max_body_size 50m",
          "client_max_body_size 50m" in src)
    check(f"{name} 仍保留 IP 限速 limit_req_zone hn_upload / hn_api",
          "zone=hn_upload" in src and "zone=hn_api" in src)
    check(f"{name} 仍保留 proxy_pass http://127.0.0.1:5000",
          "proxy_pass http://127.0.0.1:5000" in src)

# ============================================================
# B. 没有任何代码依赖 /uploads/ 这个 URL（删掉不会破功能）
# ============================================================
print("\n=== B. 代码侧零依赖 /uploads URL（下发一律走 Flask 路由）===")
_scanned = [APP_PY] + sorted(iter_frontend_files())
refs = scan_upload_url_refs()
print(f"     扫描 {len(_scanned)} 个文件（后端 app.py + 三个前端目录 + static）")
check("★ 后端与前端都没有把 `/uploads…` 当 URL 用", not refs,
      "；".join(f"{p}:{n} {s}" for p, n, s in refs[:8]))

app_src = open(APP_PY, encoding="utf-8").read()
check("后端下发走 send_file（/api/file/<id> 形态）", "send_file(" in app_src)
check("后端头像走 /api/avatar 路由（不是 /uploads/avatars 直链）",
      '"/api/avatar"' in app_src or "'/api/avatar'" in app_src)
check("App 默认 static_url_path 生效 ⇒ /uploads/… 落到 Flask 即 404",
      "static_url_path" not in app_src and re.search(r"Flask\(__name__\)", app_src) is not None)

# ============================================================
# C. DEPLOY.md 必须留下"勿直出上传目录"的警示
# ============================================================
print("\n=== C. DEPLOY.md 警示（否则下一个人照着文档又加回来）===")
deploy_src = open(DEPLOY, encoding="utf-8").read()
check("★ DEPLOY.md 含「勿直出上传目录」警示", deploy_warns(deploy_src))
check("DEPLOY.md 警示点明 md5_index.json 会随目录一起泄露",
      "md5_index.json" in deploy_src)

# ============================================================
# D. 对拍：三个检查器都必须真会报错 / 真不误报
# ============================================================
print("\n=== D. 检查器对拍（喂进坏输入必须报错，喂进合规写法不得误报）===")

_HTTP = CONFS[0]
_http_src = confs[_HTTP]

# 对拍基线：**先把已有的 /uploads/ 块摘掉**，否则基线自带一个违规块，
# 会把「加 internal 后不该报」之类的断言全部污染成假红（第一版就踩了这个坑）。
_BASE = remove_upload_locations(_http_src)
check("对拍基线构造成功（摘掉 uploads 块后不再命中）",
      not exposed_upload_locations(_BASE) and len(location_blocks(_BASE)) >= 4)
check("★ 对拍：`remove_upload_locations` 只摘 uploads、其余 location 一个不少",
      [u for u, _b, _l, _s in location_blocks(_BASE)]
      == [u for u, _b, _l, _s in location_blocks(_http_src) if not _is_upload_uri(u)],
      f"摘后={[u for u, _b, _l, _s in location_blocks(_BASE)]}")


def _append_location(src, block):
    """把一段 location 追加到最后一个 server 块内部（对拍用）。

    定位收尾大括号必须用**注释置空后**的文本 —— 注释里完全可能出现 `{ … }`
    （我在说明"原配置是什么"时就写了），直接 rfind("}") 会插到注释中间。
    """
    masked = strip_comments(src)
    i = masked.rfind("}")
    assert i > 0, "找不到 server 块的收尾大括号"
    return src[:i].rstrip() + "\n" + block + "}\n"


_DIRECT = """
    # 静态文件 — 上传的文件（可选，直接由 Nginx 提供下载）
    location /uploads/ {
        alias /opt/printer-backend/uploads/;
        expires 1h;
    }
"""

# ① 原样塞回 → 必须报，且**恰好一处**
_back = _append_location(_BASE, _DIRECT)
_hits1 = exposed_upload_locations(_back)
check("★ 对拍：把直出块原样塞回 → 必须报「外部可直接访问」（恰好一处）",
      len(_hits1) == 1 and _hits1[0][0] == "/uploads/", str(_hits1))

# ② 塞回但加 internal; → 必须不报（合规写法不得误杀）
#    注意：变异必须施加在 `_DIRECT` 上再拼装，**不能**在 `_back` 上 replace ——
#    配置里解释性注释本身含 "expires 1h; }" 字样，count=1 会命中注释而不是真块
#    （第一版就因此假红一次）。
_internal = _append_location(
    _BASE, _DIRECT.replace('expires 1h;', 'expires 1h;\n        internal;', 1))
check("★ 对拍：直出块加 `internal;` → 不得误报",
      not exposed_upload_locations(_internal),
      str(exposed_upload_locations(_internal)))

# ②' 塞回但加 deny all; → 不得误报
_deny = _append_location(
    _BASE, _DIRECT.replace('expires 1h;', 'expires 1h;\n        deny all;', 1))
check("★ 对拍：直出块加 `deny all;` → 不得误报",
      not exposed_upload_locations(_deny), str(exposed_upload_locations(_deny)))

# ②'' 反向确认变异真的生效（否则 ② 可能是因为"块压根没塞进去"才绿的假绿）
check("★ 对拍自证：② 的变异块确实含 `internal;` 且仍被解析为一个 /uploads/ location",
      "internal;" in _internal and
      any(_is_upload_uri(u) for u, _b, _l, _s in location_blocks(_internal)))

# ③ 合法写法不误报：`location = /api/upload` / `^~ /socket.io/` 不得被当成 uploads
_up = [u for u, _b, _l, _s in location_blocks(_http_src)]
check("★ 对拍：`= /api/upload` 与 `^~ /socket.io/` 都解析出纯路径",
      "/api/upload" in _up and "/socket.io/" in _up, f"uris={_up}")
check("★ 对拍：精确/前缀写法（`= /api/upload`、`^~ /socket.io/`）都不被当 uploads",
      not [_u for _u in _up if _is_upload_uri(_u) and _u != "/uploads/"], f"uris={_up}")

# ③' 换写法也拦得住：正则匹配式 location（`~`, `~*`）同样要被抓
_regex_loc = _append_location(
    _BASE, "\n    location ~* ^/uploads/ {\n        alias /opt/printer-backend/uploads/;\n    }\n")
_hits3 = exposed_upload_locations(_regex_loc)
check("★ 对拍：`location ~* ^/uploads/` 正则式写法 → 同样必须报（恰好一处）",
      len(_hits3) == 1 and _hits3[0][0] == "/uploads/", str(_hits3))

# ③'' 注释里的 location 字样不得被当真（写解释性注释时最容易踩的雷）
_annotated = _BASE.replace(
    "    # 其余 API",
    "    # 原 `location /uploads/ { alias /opt/printer-backend/uploads/; }` 已删除\n"
    "    # 其余 API", 1)
check("★ 对拍：注释里写 `location /uploads/ { … }` 不得被当成真 location",
      not exposed_upload_locations(_annotated)
      and len(location_blocks(_annotated)) == len(location_blocks(_BASE)),
      f"命中={exposed_upload_locations(_annotated)} "
      f"块数={len(location_blocks(_annotated))}/{len(location_blocks(_BASE))}")

# ④ 代码引用检查器：塞一个 /uploads URL → 必须报
_fake_py = ('a = "/uploads/md5_index.json"\n'
            'b = f"{BASE}/uploads/avatars/{oid}.jpg"\n'
            'c = "https://hn-space.cn/uploads/x.pdf"\n')
check("★ 对拍：代码里塞 3 种 `/uploads…` URL 写法 → 三条都要报出来",
      len(upload_url_references(_fake_py)) == 3,
      str(upload_url_references(_fake_py)))

# ⑤ 不误报：os.path.join("uploads", ...) 这种路径拼接不是 URL
_ok_py = ('UPLOAD_DIR = "uploads"\n'
          'P = os.path.join(UPLOAD_DIR, "md5_index.json")\n'
          'Q = os.path.relpath(fpath, UPLOAD_DIR)\n'
          'R = "/api/upload"\n')
check("★ 对拍：`UPLOAD_DIR = \"uploads\"` / join / relpath / `/api/upload` 都不得误报",
      not upload_url_references(_ok_py), str(upload_url_references(_ok_py)))

# ⑥ DEPLOY 检查器：删掉警示 → 必须报
_no_warn = re.sub(r"(?m)^.*直出.*$", "", deploy_src)
check("★ 对拍：DEPLOY.md 删掉警示段 → 必须报",
      not deploy_warns(_no_warn))

print("\n" + "=" * 56)
print("nginx 上传目录直出验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
