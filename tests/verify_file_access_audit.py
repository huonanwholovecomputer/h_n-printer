# -*- coding: utf-8 -*-
"""验收 · S3-04 文件访问审计日志（谁、读了哪个文件、何时）

依据：docs/安全加固修复计划_2026-10-23.md §S3-04（DS-24）。
交付物：
  · mobile_apps/printer-backend/file_audit.py（JSONL 追加写 / fail-open / 保留期 / 查询统计）
  · app.py 接线（download 的 ok/denied/not_found、delete 的 ok/denied/conflict/not_found、
    /api/my_files 的 list+count、新增 GET /api/admin/file_audit）
  · mobile_apps/printer-backend/ops/audit_query.py（CLI，--stats --json 是季度报告契约）
  · docs/文件访问审计说明_2026-10-24.md

本脚本：
  A. 静态：写入方式（O_APPEND/0600/0700/单次 os.write/锁）、fail-open、只记扩展名（无原名入参）、
     按日分片、保留期 180 天 + 6h 惰性、查询/统计有界、app.py 三处接线 + 管理路由角色门 +
     limit 上限 + IP 取 remote_addr、CLI 契约字段、说明文档存在。
  B. 行为：真实 Flask test_client（子进程临时后端副本）
     ① 绑定签名 URL 下载 200 → 落 download/ok（openid/file_id/task_id/ext 齐全），审计原文里
        **没有**顾客原文件名、没有绝对路径；
     ② 改坏 token → 403 且落 denied；
     ③ DELETE 成功落 delete/ok；非属主 403 落 denied；被活跃订单占用 409 落 conflict；
     ④ /api/admin/file_audit：无 token 401、guest 403、admin 200 且按 file_id 过滤；
        limit 上限生效（limit=0 → 最多 1 条）；
     ⑤ fail-open：审计目录指向"父路径是文件"的坏目录 ⇒ record 不抛、下载仍 200、
        stdout 有显式 [AUDIT] 警告（不静默）；
     /api/my_files 落 list/ok + count；保留期 prune(force=True) 删旧留新 + 6h 闸门；
     CLI --stats --json 契约、缺目录全 0、--file-id 过滤。
  C. 变异自证：
     ① 删掉 app.py 下载成功路径的审计调用 ⇒ 下载 200 但审计条目消失（B① 判据能红）；
     ② 把 file_audit.record 的 fail-open 改成 raise ⇒ 直接调用会抛（B⑤ 判据能红）。

用法：python tests/verify_file_access_audit.py     （exit 0 = 全部通过）
"""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
AUDIT_MOD = os.path.join(BACKEND, "file_audit.py")
CLI = os.path.join(BACKEND, "ops", "audit_query.py")
DOC = os.path.join(REPO, "docs", "文件访问审计说明_2026-10-24.md")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _code_only(src):
    """去掉模块 docstring：静态判据必须吃代码，不能因为文档里写了 O_APPEND 就假绿。"""
    return re.sub(r'"""[\s\S]*?"""', "", src, count=1)


def block(code, marker):
    i = code.find(marker)
    if i < 0:
        return ""
    lines = code[i:].splitlines(keepends=True)
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and not ln[:1].isspace():
            break
        out.append(ln)
    return "".join(out).replace("\r\n", "\n")


app_src = H.read_app_source()
code = H.strip_comments(app_src)
fa_src_raw = _read(AUDIT_MOD)
fa_code = _code_only(fa_src_raw)
cli_code = _read(CLI)

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")

H.chk("file_audit.py 存在且默认目录是 <backend>/logs/audit",
      os.path.isfile(AUDIT_MOD)
      and 'DEFAULT_AUDIT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "audit")'
      in fa_code)

H.chk("★ 追加写：O_APPEND|O_CREAT|O_WRONLY（+O_BINARY）+ 一次 os.write + 0600",
      "os.O_APPEND | os.O_CREAT | os.O_WRONLY" in fa_code
      and "_O_BINARY" in fa_code
      and "os.write(fd, data)" in fa_code
      and "_FILE_MODE = 0o600" in fa_code)

H.chk("目录 0700 + 进程内写锁",
      "_DIR_MODE = 0o700" in fa_code and "with _WRITE_LOCK:" in fa_code
      and "threading.Lock()" in fa_code)

record_block = block(fa_code, "def record(")
H.chk("★ fail-open：record 内部 try/except 吞异常 + print([AUDIT]) + 不 re-raise",
      "except Exception as e:" in record_block
      and 'print("[AUDIT]' in record_block
      and "return False" in record_block)

H.chk("★ record 签名只有审计字段（没有 filename/原始文件名入参）",
      'def record(action, result, file_id="", openid="", actor="", task_id=None,' in fa_code
      and 'ip="", ext="", count=None, ts=None):' in fa_code
      and "filename" not in record_block.lower())

H.chk("★ 只记扩展名：_safe_ext 去目录 + 取 splitext（误传完整文件名也只留 .pdf）",
      "os.path.basename(raw.replace" in fa_code and "os.path.splitext(raw)[1]" in fa_code)

H.chk("★ 审计模块里不出现 original_name（从源头就没有原名字段）",
      "original_name" not in fa_src_raw)

H.chk("按日分片 file_access-YYYYMMDD.jsonl（本地时区 datetime.now）",
      'FILE_PREFIX = "file_access-"' in fa_code and 'FILE_SUFFIX = ".jsonl"' in fa_code
      and 'FILE_DATE_FMT = "%Y%m%d"' in fa_code and "datetime.now()" in fa_code)

H.chk("★ 保留期：默认 180 天 + 最多每 6 小时一次 + force 强制",
      "DEFAULT_RETENTION_DAYS = 180" in fa_code
      and "PRUNE_INTERVAL_SECONDS = 6 * 3600" in fa_code
      and "def prune(retention_days=DEFAULT_RETENTION_DAYS, force=False):" in fa_code)

H.chk("查询有界（文件数/行数上限 + limit≤500）",
      "MAX_QUERY_FILES" in fa_code and "MAX_QUERY_LINES" in fa_code
      and "MAX_LIMIT = 500" in fa_code and "min(MAX_LIMIT, limit)" in fa_code)

H.chk("stats 返回 files/lines/by_action/by_result/denied",
      all(k in block(fa_code, "def stats(") for k in
          ('"files"', '"lines"', '"by_action"', '"by_result"', '"denied"')))

dl_block = block(code, "def download_file(")
del_block = block(code, "def delete_my_file(")
list_block = block(code, "def list_my_files(")
adm_block = block(code, "def admin_file_audit(")
helper_block = block(code, "def _audit_file_access(")

H.chk("app.py 顶部 import file_audit（独立模块，无 Flask 依赖）",
      "import file_audit" in code
      and "import flask" not in fa_code.lower() and "from flask" not in fa_code.lower())

H.chk("★ 下载接线：ok / denied / not_found 三态，且成功记录在 resp 之前",
      '_audit_file_access("download", "ok"' in dl_block
      and '_audit_file_access("download", "denied"' in dl_block
      and '_audit_file_access("download", "not_found"' in dl_block
      and dl_block.find('_audit_file_access("download", "ok"')
      < dl_block.find("resp = send_file("))

H.chk("★ 删除接线：ok / denied / conflict / not_found",
      all(('_audit_file_access("delete", "%s"' % r) in del_block
           for r in ("ok", "denied", "conflict", "not_found")))

H.chk("★ /api/my_files 记 list/ok + count（不逐条记 file_id）",
      '_audit_file_access("list", "ok"' in list_block and "count=len(out)" in list_block)

H.chk("管理路由 /api/admin/file_audit：login_required + admin/super_admin 角色门",
      '@app.route("/api/admin/file_audit", methods=["GET"])' in code
      and "@login_required" in code[code.find('"/api/admin/file_audit"'):
                                       code.find('"/api/admin/file_audit"') + 2000]
      and 'role not in ("admin", "super_admin")' in adm_block)

H.chk("管理路由：limit 1..500（默认 100）+ 每用户限速 30/60",
      'request.args.get("limit", 100)' in adm_block
      and "max(1, min(500, limit))" in adm_block
      and '_rate_limit_ok(f"file_audit:{g.openid}", 30, 60)' in adm_block)

_audit_code_blocks = (helper_block + adm_block).lower()
H.chk("★ IP 取 request.remote_addr（审计路径不读原始 X-Forwarded-For 头）",
      "request.remote_addr" in helper_block
      and 'headers.get("x-forwarded-for"' not in _audit_code_blocks
      and "headers.get('x-forwarded-for'" not in _audit_code_blocks)

H.chk("CLI：argparse 支持全部过滤参数 + --stats/--json/--dir",
      all(f'"--{name}"' in cli_code for name in
          ("file-id", "openid", "action", "result", "since", "until", "limit", "stats", "json", "dir")))

H.chk("★ CLI --stats --json 严格契约字段（季度报告消费）",
      all(('"%s":' % k) in cli_code for k in
          ("ok", "files", "lines", "by_action", "by_result", "denied", "since", "until")))

H.chk("说明文档存在且覆盖查询/保留期/权限/fail-open/部署验证",
      os.path.isfile(DOC)
      and all(k in _read(DOC) for k in
              ("audit_query.py", "/api/admin/file_audit", "保留期", "fail-open", "curl")))

# ============================================================
# B. 行为（子进程临时后端副本 + 真 Flask test_client）
# ============================================================
print("\n=== B. 行为（临时后端副本）===")

_SECRET = "顾客秘密甲乙丙.pdf"

_MAIN_PROBE = r'''
SECRET = "顾客秘密甲乙丙.pdf"
conn = backend.get_db()
conn.execute("INSERT INTO orders (id, file, copies, status, created_at, openid, schedule_mode, "
             "schedule_frozen, scheduled_at, target_client) VALUES "
             "(9901, 'a.pdf', 1, 'queued', '2026-10-24 10:00:00', 'owner-a', 'now', 0, '', '')")
_SQL_FILE = ("INSERT INTO files (id, original_name, saved_name, path, size, openid, page_count, "
             "page_count_verified, created_at) VALUES (?, ?, ?, ?, 10, 'owner-a', 1, 1, "
             "'2026-10-24 10:00:00')")
conn.execute(_SQL_FILE, ("f-9001", SECRET, "f-9001.pdf", "uploads/f-9001.pdf"))
conn.execute(_SQL_FILE, ("f-9002", SECRET, "f-9002.pdf", "uploads/f-9002.pdf"))
conn.execute(_SQL_FILE, ("f-9003", SECRET, "f-9003.pdf", "uploads/f-9003.pdf"))
conn.execute(_SQL_FILE, ("f-9004", SECRET, "f-9004.pdf", "uploads/f-9004.pdf"))
conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, "
             "status, created_at) VALUES (7801, 9901, 'f-9001', 'a.pdf', 1, 1, 'queued', "
             "'2026-10-24 10:00:00')")
conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, "
             "status, created_at) VALUES (7804, 9901, 'f-9004', 'a.pdf', 1, 1, 'queued', "
             "'2026-10-24 10:00:00')")
conn.commit()
conn.close()
for _n in ("f-9001.pdf", "f-9002.pdf", "f-9003.pdf", "f-9004.pdf"):
    with open(os.path.join("uploads", _n), "wb") as _fh:
        _fh.write(b"%PDF-1.4 fake")

backend.ADMIN_OPENIDS = {"admin-x"}
A = {"Authorization": "Bearer " + backend.token_serializer.dumps("owner-a")}
B = {"Authorization": "Bearer " + backend.token_serializer.dumps("owner-b")}
G = {"Authorization": "Bearer " + backend.token_serializer.dumps("guest-x")}
ADM = {"Authorization": "Bearer " + backend.token_serializer.dumps("admin-x")}


def rel(url):
    return url.split(backend.PUBLIC_BASE_URL)[1]


def audit_raw():
    adir = backend.file_audit.audit_dir()
    out = ""
    if os.path.isdir(adir):
        for name in sorted(os.listdir(adir)):
            if name.startswith("file_access-") and name.endswith(".jsonl"):
                with open(os.path.join(adir, name), encoding="utf-8") as fh:
                    out += fh.read()
    return out


# ① 绑定签名 URL 下载成功
r = http.get(rel(backend.make_download_url("f-9001", openid="owner-a", task_id=7801)))
print("DL_STATUS=%s" % r.status_code)
_rows = backend.file_audit.query(file_id="f-9001", action="download", result="ok")
print("DL_AUDIT=%d" % len(_rows))
print("DL_ENTRY=%s" % json.dumps(_rows[0] if _rows else {}, ensure_ascii=False))

# 审计原文：不允许出现原文件名 / 绝对路径；每行字段齐全
_raw = audit_raw()
print("LEAK_NAME=%d" % int("顾客秘密" in _raw))
print("LEAK_PATH=%d" % int(backend.UPLOAD_DIR in _raw or os.path.abspath("uploads") in _raw))
_fields = {"ts", "action", "result", "file_id", "openid", "actor", "task_id", "ip", "ext", "count"}
_schema = 1
for _line in _raw.splitlines():
    _line = _line.strip()
    if not _line:
        continue
    try:
        _e = json.loads(_line)
    except ValueError:
        _schema = 0
        break
    if not _fields.issubset(set(_e)):
        _schema = 0
        break
print("SCHEMA_OK=%d" % _schema)

# ② 改坏 token
_u = rel(backend.make_download_url("f-9001", openid="owner-a", task_id=7801))
r = http.get(_u[:-4] + "zzzz")
print("BAD_STATUS=%s" % r.status_code)
print("BAD_AUDIT=%d" % len(backend.file_audit.query(file_id="f-9001", action="download",
                                                    result="denied")))

# ③ 删除：成功 / 非属主 / 活跃订单 409
r = http.delete("/api/file/f-9002", headers=A)
print("DEL_STATUS=%s" % r.status_code)
print("DEL_AUDIT=%d" % len(backend.file_audit.query(file_id="f-9002", action="delete",
                                                   result="ok")))
r = http.delete("/api/file/f-9003", headers=B)
print("DEL_OTHER_STATUS=%s" % r.status_code)
print("DEL_OTHER_AUDIT=%d" % len(backend.file_audit.query(file_id="f-9003", action="delete",
                                                         result="denied")))
r = http.delete("/api/file/f-9004", headers=A)
print("DEL_BUSY_STATUS=%s" % r.status_code)
print("DEL_BUSY_AUDIT=%d" % len(backend.file_audit.query(file_id="f-9004", action="delete",
                                                        result="conflict")))

# ④ 管理查询权限门 + 过滤 + limit 上限
print("ADMIN_NOAUTH=%s" % http.get("/api/admin/file_audit").status_code)
print("ADMIN_GUEST=%s" % http.get("/api/admin/file_audit", headers=G).status_code)
r = http.get("/api/admin/file_audit?file_id=f-9001&action=download&limit=50&stats=1", headers=ADM)
_j = r.get_json() or {}
print("ADMIN_STATUS=%s" % r.status_code)
print("ADMIN_COUNT=%d" % int(_j.get("count") or 0))
print("ADMIN_ALL_MATCH=%d" % int(bool(_j.get("entries")) and all(
    (e.get("file_id") == "f-9001" and e.get("action") == "download")
    for e in _j.get("entries") or [])))
print("ADMIN_STATS=%d" % int(isinstance(_j.get("stats"), dict)
                             and "denied" in (_j.get("stats") or {})))
print("ADMIN_LIMIT_BIG=%s" % http.get("/api/admin/file_audit?limit=9999", headers=ADM).status_code)
print("ADMIN_LIMIT_ZERO=%d" % int((http.get("/api/admin/file_audit?limit=0", headers=ADM)
                                   .get_json() or {}).get("count", -1)))

# 「我的文件」列表 → list/ok + count
r = http.get("/api/my_files?per_page=50", headers=A)
_j = r.get_json() or {}
_rows = backend.file_audit.query(openid="owner-a", action="list", result="ok")
print("LIST_STATUS=%s" % r.status_code)
print("LIST_AUDIT=%d" % len(_rows))
print("LIST_COUNT=%d" % (int(_rows[0]["count"]) if _rows else -1))
print("LIST_EXPECT=%d" % len(_j.get("files") or []))

# ⑤ fail-open：审计目录不可写（父路径是个文件）⇒ record 不抛、下载仍 200、有显式警告
_blocker = os.path.abspath("blocker_file")
with open(_blocker, "wb") as _fh:
    _fh.write(b"x")
os.environ["HN_FILE_AUDIT_DIR"] = os.path.join(_blocker, "audit")
_raised = 0
try:
    backend.file_audit.record("download", "ok", file_id="f-failopen")
except Exception:
    _raised = 1
print("RECORD_RAISED=%d" % _raised)
r = http.get(rel(backend.make_download_url("f-9001", openid="owner-a", task_id=7801)))
print("FAILOPEN_DL=%s" % r.status_code)
os.environ.pop("HN_FILE_AUDIT_DIR", None)
'''

_main_tmp, _main_dst = H.make_copy("hn_audit_main_")
proc = H.run_probe(_main_dst, _MAIN_PROBE, timeout=240)
if proc.returncode != 0:
    print("probe stdout tail:", (proc.stdout or "")[-1500:])
    print("probe stderr tail:", (proc.stderr or "")[-1500:])

V = lambda k: H.probe_value(proc, k)  # noqa: E731

H.chk("子进程探针正常退出", proc.returncode == 0, "rc=%s" % proc.returncode)
H.chk("① 绑定签名 URL 下载 200", V("DL_STATUS") == "200", V("DL_STATUS"))
H.chk("★ ① 落一条 download/ok 审计", V("DL_AUDIT") == "1", V("DL_AUDIT"))

try:
    _entry = json.loads(V("DL_ENTRY") or "{}")
except ValueError:
    _entry = {}
H.chk("★ ① 审计条目含 openid / file_id / task_id / actor / ext",
      _entry.get("openid") == "owner-a" and _entry.get("file_id") == "f-9001"
      and _entry.get("task_id") == 7801 and _entry.get("actor") == "signed_url"
      and _entry.get("ext") == ".pdf", _entry)
H.chk("① ts 是带时区偏移的 ISO8601",
      re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
               str(_entry.get("ts") or "")) is not None, _entry.get("ts"))
H.chk("① 审计条目记录了真实 IP（request.remote_addr）", bool(_entry.get("ip")), _entry.get("ip"))
H.chk("★ ① 审计原文**不含**顾客原文件名", V("LEAK_NAME") == "0", V("LEAK_NAME"))
H.chk("★ ① 审计原文**不含**绝对路径", V("LEAK_PATH") == "0", V("LEAK_PATH"))
H.chk("① 每行 JSON 字段齐全（ts/action/result/file_id/openid/actor/task_id/ip/ext/count）",
      V("SCHEMA_OK") == "1", V("SCHEMA_OK"))

# 落盘字节：O_BINARY 生效 ⇒ 纯 \n、单次写不产生 \r\n（Windows 文本模式会翻）
_adir = os.path.join(_main_dst, "logs", "audit")
_bad_nl = 0
_shards = sorted(os.listdir(_adir)) if os.path.isdir(_adir) else []
for _n in _shards:
    if not _n.endswith(".jsonl"):
        continue
    with open(os.path.join(_adir, _n), "rb") as _fh:
        _b = _fh.read()
    if b"\r\n" in _b or (bool(_b) and not _b.endswith(b"\n")):
        _bad_nl += 1
H.chk("★ ① 落盘是纯 \\n 结尾的 JSONL（O_BINARY：不出现 \\r\\n）",
      bool(_shards) and _bad_nl == 0, (_shards, _bad_nl))

H.chk("② 改坏 token → 403 且落 denied", V("BAD_STATUS") == "403" and V("BAD_AUDIT") == "1",
      (V("BAD_STATUS"), V("BAD_AUDIT")))

H.chk("③ DELETE 成功 200 且落 delete/ok",
      V("DEL_STATUS") == "200" and V("DEL_AUDIT") == "1", (V("DEL_STATUS"), V("DEL_AUDIT")))
H.chk("③ 非属主删除 403 且落 delete/denied",
      V("DEL_OTHER_STATUS") == "403" and V("DEL_OTHER_AUDIT") == "1",
      (V("DEL_OTHER_STATUS"), V("DEL_OTHER_AUDIT")))
H.chk("③ 被活跃订单占用 409 且落 delete/conflict",
      V("DEL_BUSY_STATUS") == "409" and V("DEL_BUSY_AUDIT") == "1",
      (V("DEL_BUSY_STATUS"), V("DEL_BUSY_AUDIT")))

H.chk("④ 管理查询：无 token 401 / guest 403",
      V("ADMIN_NOAUTH") == "401" and V("ADMIN_GUEST") == "403",
      (V("ADMIN_NOAUTH"), V("ADMIN_GUEST")))
H.chk("★ ④ admin 200 且能按 file_id 过滤（返回条目全部匹配）",
      V("ADMIN_STATUS") == "200" and int(V("ADMIN_COUNT") or 0) >= 1
      and V("ADMIN_ALL_MATCH") == "1",
      (V("ADMIN_STATUS"), V("ADMIN_COUNT"), V("ADMIN_ALL_MATCH")))
H.chk("④ stats=1 时附统计块（含 denied）", V("ADMIN_STATS") == "1", V("ADMIN_STATS"))
H.chk("④ ★ limit 上限生效：limit=9999 仍 200、limit=0 被夹到 1 条",
      V("ADMIN_LIMIT_BIG") == "200" and int(V("ADMIN_LIMIT_ZERO") or -1) <= 1,
      (V("ADMIN_LIMIT_BIG"), V("ADMIN_LIMIT_ZERO")))

H.chk("★ /api/my_files 落 list/ok 且 count=本页条数",
      V("LIST_STATUS") == "200" and V("LIST_AUDIT") == "1"
      and V("LIST_COUNT") == V("LIST_EXPECT"),
      (V("LIST_STATUS"), V("LIST_AUDIT"), V("LIST_COUNT"), V("LIST_EXPECT")))

H.chk("★ ⑤ fail-open：坏目录下直接 record 不抛", V("RECORD_RAISED") == "0", V("RECORD_RAISED"))
H.chk("★ ⑤ 审计目录不可写时下载仍 200", V("FAILOPEN_DL") == "200", V("FAILOPEN_DL"))
H.chk("★ ⑤ 失败是显式的（stdout 有 [AUDIT] 警告，不静默）",
      "[AUDIT]" in (proc.stdout or ""), (proc.stdout or "")[-300:])

# ---------------------------------------------------------------- 保留期
print("\n=== B-2. 保留期惰性清理（prune）===")


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("file_audit_under_test", AUDIT_MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fa = _load_audit_module()
_ret_dir = tempfile.mkdtemp(prefix="hn_audit_ret_")
os.environ["HN_FILE_AUDIT_DIR"] = _ret_dir
_today = date.today()
_old = _today - timedelta(days=200)
_old_path = os.path.join(_ret_dir, "file_access-%s.jsonl" % _old.strftime("%Y%m%d"))
_today_path = os.path.join(_ret_dir, "file_access-%s.jsonl" % _today.strftime("%Y%m%d"))
for _p in (_old_path, _today_path):
    with open(_p, "w", encoding="utf-8") as _fh:
        _fh.write('{"ts":"2026-01-01T00:00:00+08:00","action":"download","result":"ok"}\n')

fa._PRUNE_STATE["last"] = 0.0
_removed = fa.prune(force=True)
H.chk("⑥ prune(force=True) 删除 200 天前的分片、保留当天分片",
      _removed == 1 and not os.path.exists(_old_path) and os.path.exists(_today_path),
      (_removed, os.path.exists(_old_path), os.path.exists(_today_path)))

_old2 = os.path.join(_ret_dir, "file_access-%s.jsonl"
                     % (_today - timedelta(days=201)).strftime("%Y%m%d"))
with open(_old2, "w", encoding="utf-8") as _fh:
    _fh.write('{"ts":"2026-01-01T00:00:00+08:00","action":"download","result":"ok"}\n')
fa._PRUNE_STATE["last"] = time.time()
H.chk("⑥ 6 小时闸门：窗口内非 force prune 不再扫目录（旧分片仍在）",
      fa.prune() == 0 and os.path.exists(_old2))
fa._PRUNE_STATE["last"] = 0.0
H.chk("⑥ 窗口过后非 force prune 照常清理", fa.prune() == 1 and not os.path.exists(_old2))
os.environ.pop("HN_FILE_AUDIT_DIR", None)

# ---------------------------------------------------------------- CLI
print("\n=== B-3. CLI（--stats --json 契约 / 缺目录 / 过滤）===")
_cli_dir = tempfile.mkdtemp(prefix="hn_audit_cli_")


def _write_jsonl(name, entries):
    path = os.path.join(_cli_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


_write_jsonl("file_access-20261003.jsonl", [
    {"ts": "2026-10-01T10:00:00+08:00", "action": "download", "result": "ok",
     "file_id": "f-cli", "openid": "o-cli", "actor": "signed_url", "task_id": 1,
     "ip": "1.2.3.4", "ext": ".pdf", "count": None},
    {"ts": "2026-10-02T10:00:00+08:00", "action": "download", "result": "denied",
     "file_id": "f-cli", "openid": "o-cli", "actor": "signed_url", "task_id": None,
     "ip": "1.2.3.4", "ext": "", "count": None},
    {"ts": "2026-10-03T10:00:00+08:00", "action": "delete", "result": "ok",
     "file_id": "f-cli2", "openid": "o-cli", "actor": "user", "task_id": None,
     "ip": "1.2.3.4", "ext": ".pdf", "count": None},
])
_write_jsonl("file_access-20260901.jsonl", [
    {"ts": "2026-09-01T10:00:00+08:00", "action": "download", "result": "ok",
     "file_id": "f-cli2", "openid": "o-cli", "actor": "signed_url", "task_id": 2,
     "ip": "1.2.3.4", "ext": ".pdf", "count": None},
])

_env = dict(os.environ)
_env["PYTHONPATH"] = ""
_env["PYTHONUTF8"] = "1"


def _run_cli(args):
    return subprocess.run([sys.executable, CLI] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=_env, timeout=120)


_p = _run_cli(["--dir", _cli_dir, "--stats", "--json"])
_stats_payload = {}
if _p.returncode == 0:
    try:
        _stats_payload = json.loads((_p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        _stats_payload = {}
H.chk("⑦ CLI --stats --json 退出码 0 且是合法 JSON", _p.returncode == 0 and bool(_stats_payload),
      (_p.returncode, (_p.stdout or "")[-300:], (_p.stderr or "")[-300:]))
H.chk("★ ⑦ 契约字段齐全且值正确（files=2/lines=4/denied=1）",
      set(_stats_payload) == {"ok", "files", "lines", "by_action", "by_result",
                              "denied", "since", "until"}
      and _stats_payload.get("ok") is True
      and _stats_payload.get("files") == 2 and _stats_payload.get("lines") == 4
      and _stats_payload.get("denied") == 1
      and _stats_payload.get("by_action") == {"download": 3, "delete": 1}
      and _stats_payload.get("by_result") == {"ok": 3, "denied": 1}, _stats_payload)

_p = _run_cli(["--dir", _cli_dir, "--stats", "--json",
               "--since", "2026-10-01", "--until", "2026-10-31"])
_filtered = {}
if _p.returncode == 0:
    try:
        _filtered = json.loads((_p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        _filtered = {}
H.chk("⑦ --since/--until 生效（10 月只有 3 条，denied 仍 1）",
      _filtered.get("lines") == 3 and _filtered.get("denied") == 1
      and _filtered.get("since") == "2026-10-01" and _filtered.get("until") == "2026-10-31",
      _filtered)

_missing = os.path.join(_cli_dir, "nope")
_p = _run_cli(["--dir", _missing, "--stats", "--json"])
_missing_payload = {}
if _p.returncode == 0:
    try:
        _missing_payload = json.loads((_p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        _missing_payload = {}
H.chk("★ ⑦ 缺目录 ⇒ ok=true 且全 0（不崩溃）",
      _p.returncode == 0 and _missing_payload.get("ok") is True
      and _missing_payload.get("files") == 0 and _missing_payload.get("lines") == 0
      and _missing_payload.get("denied") == 0,
      (_p.returncode, _missing_payload, (_p.stderr or "")[-200:]))

_p = _run_cli(["--dir", _cli_dir, "--file-id", "f-cli", "--json"])
_q = {}
if _p.returncode == 0:
    try:
        _q = json.loads((_p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        _q = {}
H.chk("⑦ --file-id 过滤（f-cli 两条，JSON 输出）",
      _q.get("ok") is True and _q.get("count") == 2
      and all(e.get("file_id") == "f-cli" for e in _q.get("entries") or []), _q.get("count"))

_p = _run_cli(["--dir", _cli_dir, "--result", "denied"])
H.chk("⑦ 人类可读输出包含 denied 记录", "denied" in (_p.stdout or "")
      and "f-cli" in (_p.stdout or ""), (_p.stdout or "")[-300:])

# ============================================================
# C. 变异自证
# ============================================================
print("\n=== C. 变异自证 ===")

_MUT_DL_BODY = r'''
conn = backend.get_db()
conn.execute("INSERT INTO orders (id, file, copies, status, created_at, openid, schedule_mode, "
             "schedule_frozen, scheduled_at, target_client) VALUES "
             "(9901, 'a.pdf', 1, 'queued', '2026-10-24 10:00:00', 'owner-a', 'now', 0, '', '')")
conn.execute("INSERT INTO files (id, original_name, saved_name, path, size, openid, page_count, "
             "page_count_verified, created_at) VALUES ('f-9001', '顾客秘密甲乙丙.pdf', "
             "'f-9001.pdf', 'uploads/f-9001.pdf', 10, 'owner-a', 1, 1, '2026-10-24 10:00:00')")
conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, "
             "status, created_at) VALUES (7801, 9901, 'f-9001', 'a.pdf', 1, 1, 'queued', "
             "'2026-10-24 10:00:00')")
conn.commit()
conn.close()
with open(os.path.join("uploads", "f-9001.pdf"), "wb") as _fh:
    _fh.write(b"%PDF-1.4 fake")
r = http.get(backend.make_download_url("f-9001", openid="owner-a", task_id=7801)
             .split(backend.PUBLIC_BASE_URL)[1])
print("STATUS=%s" % r.status_code)
print("AUDIT_OK=%d" % len(backend.file_audit.query(file_id="f-9001", action="download",
                                                   result="ok")))
'''

# 对照：正常副本上同一探针必须 AUDIT_OK=1（主探针已证），变异副本必须 0。
_mut_tmp, _mut_dst = H.mutated_copy([
    ('    _audit_file_access("download", "ok", file_id=file_id, openid=bound_openid,\n'
     '                       task_id=bound_task, actor="signed_url",\n'
     '                       ext=_audit_ext(row["original_name"] or row["path"]))\n',
     '    # 变异：删掉下载成功路径的审计调用\n'),
], prefix="hn_audit_mut1_")
_pm = H.run_probe(_mut_dst, _MUT_DL_BODY, timeout=180)
H.chk("★ 变异①：删掉下载成功路径的审计调用 ⇒ 下载仍 200、但审计条目消失（B① 能红）",
      H.probe_value(_pm, "STATUS") == "200" and H.probe_value(_pm, "AUDIT_OK") == "0",
      "STATUS=%r AUDIT_OK=%r stderr=%s"
      % (H.probe_value(_pm, "STATUS"), H.probe_value(_pm, "AUDIT_OK"),
         (_pm.stderr or "")[-400:]))


def _mutate_file(dst, rel_path, old, new):
    path = os.path.join(dst, rel_path)
    with open(path, "r", encoding="utf-8", newline="") as f:
        src = f.read()
    if src.count(old) < 1:
        raise AssertionError("变异锚点未命中：%s" % rel_path)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src.replace(old, new, 1))


# 变异②：把 record 的 fail-open 改成"抛出去"
_mut_tmp2, _mut_dst2 = H.make_copy("hn_audit_mut2_")
_mutate_file(
    _mut_dst2, "file_audit.py",
    '    except Exception as e:  # noqa: BLE001  ← fail-open：审计失败绝不影响主流程\n'
    '        # 只打异常类名（异常文本里可能带绝对路径，S2-11 口径）。\n'
    '        print("[AUDIT] 文件访问审计写入失败（已忽略，不影响主流程）: %s" % type(e).__name__)\n'
    '        return False',
    '    except Exception as e:  # 变异：不再吞异常\n        raise')

_MUT_FAILOPEN_BODY = _MUT_DL_BODY + r'''
_blocker = os.path.abspath("blocker_file")
with open(_blocker, "wb") as _fh:
    _fh.write(b"x")
os.environ["HN_FILE_AUDIT_DIR"] = os.path.join(_blocker, "audit")
_raised = 0
try:
    backend.file_audit.record("download", "ok", file_id="f-mut")
except Exception:
    _raised = 1
print("RECORD_RAISED=%d" % _raised)
'''
_pm2 = H.run_probe(_mut_dst2, _MUT_FAILOPEN_BODY, timeout=180)
H.chk("★ 变异②：把 fail-open 改成 raise ⇒ 直接调用 record 会抛出（B⑤ 判据能红）",
      H.probe_value(_pm2, "RECORD_RAISED") == "1"
      and H.probe_value(_pm2, "STATUS") == "200",
      "RECORD_RAISED=%r STATUS=%r stderr=%s"
      % (H.probe_value(_pm2, "RECORD_RAISED"), H.probe_value(_pm2, "STATUS"),
         (_pm2.stderr or "")[-400:]))

H.finish()
