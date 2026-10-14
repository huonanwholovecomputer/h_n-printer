# -*- coding: utf-8 -*-
"""审计修复验收 · 🔴-A7：PDF 缓存 key 的引擎后缀判定漂移

事故形状：同一个文件在**写侧**与**读侧**算出不同的缓存键 ⇒ 缓存永不命中。
 · 写侧（预转换完成后写索引）：只看 `job.engine`，`"word"`/`"wps"` 就加 `_e{engine}` 后缀；
 · 读侧（下次添加同文件时查缓存）与打印侧：还要求扩展名是 `.docx` 才加后缀。
而 `job.engine` 对**图片/PDF/TXT 也默认就是 `"word"`**（只有 `.doc/.docx` 才去改它）
⇒ 本地拖入的图片写成 `{md5}_eword.pdf`、读时按 `{md5}.pdf` 找，**永不命中**：
每添加一次就重新转换一次（现场现象：7 个图片渲染 18+ 次）。云端下载的文件走另一条
路径（无后缀），反而能命中 —— 这就是"云端不反复转换、本地拖入才反复转换"的分裂来源。

修法：判定收敛为**唯一函数** `cloud_client.cache_engine_for(path_or_ext, engine)`
（仅 `.docx` 且引擎 ∈ {word, wps} 才返回引擎名），写/读/打印三侧共用。

本脚本覆盖：
 1. cache_engine_for 的判定表（含大小写、路径形式、边界）；
 2. 「改前两套口径会产生不同键」的缺陷复现 + 「改后两侧逐字相同」；
 3. 静态守卫：gui.py 中所有缓存 API 调用的引擎参数都必须来自 cache_engine_for
    （防未来再次漂移回裸 engine）；
 4. 真实 CloudClient 的读写配对（图片/docx/doc 三组，写进去必须读得出来）；
 5. 历史脏条目（非 docx 却带 _eword/_ewps）被一次性清理，有效条目保留。
"""
import ast
import io
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import cloud_client as cc                          # noqa: E402
from cloud_client import CloudClient, cache_engine_for, pdf_cache_key  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


MD5 = "0123456789abcdef0123456789abcdef"

# ─────────────────────────────────────────────────────────────
print("=== 1. cache_engine_for 判定表（唯一口径）===")
CASES = [
    # (输入（路径或裸扩展名）, 引擎, 期望)
    (".jpg", "word", ""),          # 图片：job.engine 就是 "word"，但绝不能加后缀（A7 根因）
    (".pdf", "word", ""),          # PDF 同上
    ("a.txt", "word", ""),
    (".docx", "word", "word"),     # docx + word：按引擎隔离（WPS 布局被 Word 渲染会出错）
    (".docx", "wps", "wps"),
    ("D:/x/y/报告.docx", "wps", "wps"),
    ("A.DOCX", "word", "word"),    # 大小写不敏感
    (".doc", "word", ""),          # 老格式 .doc 与引擎无关（三侧一律无后缀）
    (".docx", "libreoffice", ""),  # 非 word/wps 引擎 → 无后缀
    (".docx", "", ""),
    (".docx", "Word", ""),         # 引擎名大小写敏感（上游只产出小写）
    ("", "word", ""),
    (None, "wps", ""),
]
bad = []
for src, eng, want in CASES:
    got = cache_engine_for(src, eng)
    if got != want:
        bad.append(f"{src!r}+{eng!r} 得 {got!r} 期望 {want!r}")
check("判定表全部符合", not bad, "; ".join(bad))


# ─────────────────────────────────────────────────────────────
print("\n=== 2. 缺陷复现：改前两套口径的键不同；改后两侧逐字相同 ===")


def old_write_engine(engine):
    """改前**写侧**口径（gui.py 预转换完成处）：只看引擎。"""
    return engine if engine in ("word", "wps") else ""


def old_read_engine(path, engine):
    """改前**读侧/打印侧**口径：还要求扩展名是 .docx。"""
    ext = os.path.splitext(str(path or ""))[1].lower()
    return engine if (ext == ".docx" and engine in ("word", "wps")) else ""


# 五组输入（计划要求的验收面）
FIVE = [("photo.jpg", "word"), ("doc.pdf", "word"),
        ("report.docx", "word"), ("report.docx", "wps"), ("legacy.doc", "word")]

broken = []
for path, eng in FIVE:
    ow = pdf_cache_key(MD5, "", old_write_engine(eng))
    orr = pdf_cache_key(MD5, "", old_read_engine(path, eng))
    if ow != orr:
        broken.append(f"{path}+{eng}: 写 {ow} != 读 {orr}")
# 旧写侧只看引擎（"word" 一律加后缀），旧读侧还要求 .docx
# ⇒ 非 docx 的三组（jpg / pdf / doc）键不一致；两组 docx 恰好一致（所以缺陷长期隐身）
check("改前口径在非 docx 上确实不一致（缺陷复现：图片/PDF/老 doc 三组）",
      len(broken) == 3 and all((".jpg" in b or ".pdf" in b or ".doc " in b or "legacy" in b)
                               for b in broken),
      "; ".join(broken) + f"  （不一致组数={len(broken)}，期望 3）")
_docx_pairs = [pdf_cache_key(MD5, "", old_write_engine(e)) == pdf_cache_key(MD5, "", old_read_engine(p, e))
               for p, e in FIVE if p.endswith(".docx")]
check("两组 docx 旧口径本来就一致（解释缺陷为何只砸非 docx）",
      all(_docx_pairs) and len(_docx_pairs) == 2, f"docx 组={_docx_pairs}")

same = []
for path, eng in FIVE:
    w = pdf_cache_key(MD5, "", cache_engine_for(path, eng))
    r = pdf_cache_key(MD5, "", cache_engine_for(path, eng))
    if w != r:
        same.append(f"{path}+{eng}: {w} != {r}")
check("改后三侧共用一个函数 ⇒ 写/读键逐字相同", not same, "; ".join(same))

# 断言"同一函数"是真的同一函数（不是本脚本里碰巧写了两遍）
key_photo = pdf_cache_key(MD5, "", cache_engine_for("photo.jpg", "word"))
check("图片的键回到纯 MD5（与云端下载路径一致）", key_photo == MD5, key_photo)
key_docx = pdf_cache_key(MD5, "", cache_engine_for("report.docx", "word"))
check("docx 的键保留引擎后缀", key_docx == MD5 + "_eword", key_docx)


# ─────────────────────────────────────────────────────────────
print("\n=== 3. 静态守卫：gui.py 缓存 API 的引擎参数必须来自 cache_engine_for ===")
GUI_SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_tree = ast.parse(GUI_SRC)

# 收集「被赋值为 cache_engine_for(...)」的变量名（含属性赋值的简单名）
ENGINE_VARS = set()
for node in ast.walk(_tree):
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
            and getattr(node.value.func, "id", "") == "cache_engine_for":
        for t in node.targets:
            if isinstance(t, ast.Name):
                ENGINE_VARS.add(t.id)

# 引擎是这三个缓存 API 的最后一个位置参数
CACHE_APIS = {"pdf_cache_key": [2], "_get_cached_pdf": [2], "_save_pdf_to_cache": [6]}


def _is_engine_ok(expr):
    """引擎实参是否可接受：cache_engine_for(...) 调用 / 由它赋值的变量 / 空串常量。"""
    if isinstance(expr, ast.Call) and getattr(expr.func, "id", "") == "cache_engine_for":
        return True
    if isinstance(expr, ast.Constant) and expr.value == "":
        return True
    if isinstance(expr, ast.Name) and expr.id in ENGINE_VARS:
        return True
    return False


offenders = []
for node in ast.walk(_tree):
    if not isinstance(node, ast.Call):
        continue
    fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    if fname not in CACHE_APIS:
        continue
    # 关键字传参优先
    kw = {k.arg: k.value for k in node.keywords if k.arg}
    idxs = CACHE_APIS[fname]
    for i in idxs:
        # 只在**确实传了**引擎位置参数时才检查（省略 = 取缺省 ""，合法）
        if i < len(node.args) and not _is_engine_ok(node.args[i]):
            offenders.append(f"第 {i+1} 个位置参数: {ast.unparse(node.args[i])[:70]}")
    if "engine" in kw and not _is_engine_ok(kw["engine"]):
        offenders.append(f"engine= 关键字: {ast.unparse(kw['engine'])[:70]}")

check("cache_engine_for 在 gui.py 中被实际使用（≥3 处赋值）",
      len(ENGINE_VARS) >= 1 and GUI_SRC.count("cache_engine_for(") >= 3,
      f"赋值变量={sorted(ENGINE_VARS)}，出现次数={GUI_SRC.count('cache_engine_for(')}")
check("所有缓存 API 调用的引擎参数都来自 cache_engine_for（防再次漂移）",
      not offenders, "; ".join(offenders[:6]))

# 旧写法必须绝迹
OLD_PATTERNS = ['_cache_engine if _cache_engine in (', 'engine if engine in ("word", "wps")']
residue = [p for p in OLD_PATTERNS if p in GUI_SRC]
check("gui.py 中已无旧的裸引擎判定写法", not residue, "; ".join(residue))


# ─────────────────────────────────────────────────────────────
print("\n=== 4. 端到端：真实 CloudClient 写进去必须读得出来 ===")
TMP = tempfile.mkdtemp(prefix="hn_cache_key_")
client = CloudClient(api_url="", ws_url="", token="t", client_id="ck-verify")
client._CACHE_DIR = TMP      # property 的覆盖入口（缓存目录隔离到临时目录）
# 禁止 _save_pdf_to_cache 触发的后台清理线程介入（它 5 秒后会动同一份 index，
# 与第 5 节的断言抢时序）
client._schedule_cache_cleanup = lambda: None


def _make_pdf(path):
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def roundtrip(name, src_ext, file_name, engine):
    """写入缓存 → 按同一口径读回，返回是否命中。"""
    md5 = f"{name:0<32}"[:32]
    pdf = os.path.join(TMP, f"src_{name}.pdf")
    _make_pdf(pdf)
    eng = cache_engine_for(file_name, engine)
    client._save_pdf_to_cache(md5, pdf, file_name, src_ext, 3, "", eng)
    hit, meta = client._get_cached_pdf(md5, "", cache_engine_for(file_name, engine))
    return hit, meta


for tag, src_ext, fname, eng in [("jpg", ".jpg", "photo.jpg", "word"),
                                 ("docx_word", ".docx", "report.docx", "word"),
                                 ("docx_wps", ".docx", "report.docx", "wps"),
                                 ("doc", ".doc", "legacy.doc", "word")]:
    hit, meta = roundtrip(tag, src_ext, fname, eng)
    check(f"读写配对命中：{fname} + {eng}", bool(hit) and meta.get("engine", "?") is not None,
          f"hit={hit!r}")

# 旧口径对照：图片用改前写侧口径写、改前读侧口径读 ⇒ 必然 miss（证明缺陷真实）
md5_legacy = "f" * 32
pdf_legacy = os.path.join(TMP, "src_legacy.pdf")
_make_pdf(pdf_legacy)
client._save_pdf_to_cache(md5_legacy, pdf_legacy, "photo.jpg", ".jpg", 1, "",
                          old_write_engine("word"))          # 改前写侧：_eword
legacy_hit, _ = client._get_cached_pdf(md5_legacy, "", old_read_engine("photo.jpg", "word"))
check("旧口径（写 _eword / 读无后缀）确实读不出——缺陷复现", legacy_hit is None,
      f"hit={legacy_hit!r}")


# ─────────────────────────────────────────────────────────────
print("\n=== 5. 历史脏条目一次性清理 ===")
import datetime  # noqa: E402
_NOW = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")   # 用当前时间：只验脏清理，不混入过期清理
index_path = os.path.join(TMP, "index.json")
entries = {
    MD5 + "_eword": {"source_ext": ".jpg", "created_at": _NOW},    # 脏
    MD5 + "_ewps": {"source_ext": ".png", "created_at": _NOW},     # 脏
    "a" * 32 + "_eword": {"source_ext": ".docx", "created_at": _NOW},   # 有效
    "b" * 32: {"source_ext": ".jpg", "created_at": _NOW},           # 有效
    "c" * 32 + "_landscape": {"source_ext": ".jpg", "created_at": _NOW},  # 有效
}
for k in entries:
    _make_pdf(os.path.join(TMP, f"{k}.pdf"))
with open(index_path, "w", encoding="utf-8") as f:
    json.dump(entries, f, ensure_ascii=False)

client._CACHE_RETENTION_HOURS = 168
client._cleanup_pdf_cache()

after = json.load(open(index_path, encoding="utf-8"))
left = set(after)
check("脏条目（非 docx + _eword/_ewps）已清除",
      not any(k.endswith(("_eword", "_ewps")) and after.get(k, {}).get("source_ext") != ".docx"
              for k in left),
      f"剩余={sorted(left)}")
check("docx 的 _eword 有效条目被保留", "a" * 32 + "_eword" in left, f"剩余={sorted(left)}")
check("无后缀图片条目与方向后缀条目被保留",
      "b" * 32 in left and "c" * 32 + "_landscape" in left, f"剩余={sorted(left)}")
check("脏条目的 PDF 文件一并删除",
      not os.path.isfile(os.path.join(TMP, MD5 + "_eword.pdf"))
      and not os.path.isfile(os.path.join(TMP, MD5 + "_ewps.pdf")))
check("有效条目的 PDF 文件保留",
      os.path.isfile(os.path.join(TMP, "a" * 32 + "_eword.pdf"))
      and os.path.isfile(os.path.join(TMP, "b" * 32 + ".pdf")))


print("\n" + ("全部通过" if ok_all else "存在失败项"))
sys.exit(0 if ok_all else 1)
