"""S0-01 复现 · staging 上 16 条 WebSocket 饱和 + 边缘补丁验收（16 条）

依据：docs/安全加固修复计划_2026-10-23.md §S0-01
  · 关键改动：/socket.io/ limit_conn 6 + limit_req 30r/m burst 20 + proxy_read_timeout 120s；
    /api/ body 2m + burst 150 + limit_conn 12；/api/upload 50m + limit_conn 6；
    /api/profile 3m + limit_conn 4；/api/login、/api/device_login 20r/m burst 5 + limit_conn 3；
    / body 1m + burst 20；http{} 10s/10s/30s/30s + reset_timedout_connection + server_tokens off +
    limit_req_status/limit_conn_status 429。
  · 验收原文（第 258 行）：`nginx -t`；staging 开 16 条 WS → 第 7 条起 429、`/api/ping` 仍 200；
    6 个待验证 Word 同时轮询不出现 503；50 MB POST → nginx 413 且 client_body_temp_path 无落盘；
    `/` 的 404 洪水被限速；两台真机重连正常。
  · §11.2（第 557 行）：第 7 条并发 WS 起 429/403（limit_conn 6 + 握手 gate），
    同时 /api/ping p95 <100 ms。

用法（先 staging_up.py）：
  PYTHONPATH= PYTHONUTF8=1 "<python311>" tools/staging/s0_01_saturation.py

输出：_tmp_staging/results/s0_01.json + 控制台逐条 PASS/FAIL。
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import staging_common as sc  # noqa: E402

BASE = sc.edge_base()
RESULTS = []
T0 = time.time()


def log(msg):
    print("[%6.1fs] %s" % (time.time() - T0, msg), flush=True)


# ---------------- 证据工具 ----------------
def nginx_access_tail(n=400):
    try:
        with open(sc.NGINX_ACCESS_LOG, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except OSError:
        return []


def access_lines_matching(pred, limit=8):
    out = []
    for ln in reversed(nginx_access_tail(600)):
        if pred(ln):
            out.append(ln)
            if len(out) >= limit:
                break
    return list(reversed(out))


def nginx_error_tail(n=6):
    try:
        with open(sc.NGINX_ERROR_LOG, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except OSError:
        return []


def temp_dir_listing():
    d = os.path.join(sc.NGINX_TEMP, "client_body_temp")
    try:
        items = []
        for name in os.listdir(d):
            p = os.path.join(d, name)
            items.append({"name": name, "bytes": os.path.getsize(p)})
        return items
    except OSError:
        return []


def record(cid, title, passed, criteria, evidence, notes=""):
    row = {"id": cid, "title": title, "pass": bool(passed), "criteria": criteria,
           "evidence": evidence, "notes": notes, "at": time.strftime("%H:%M:%S")}
    RESULTS.append(row)
    log("%-3s %-6s %s" % (cid, "PASS" if passed else "FAIL", title))
    if not passed and notes:
        log("      ↳ %s" % notes)
    return row


# ---------------- 1. nginx -t ----------------
def check_01():
    code, out = sc.nginx_test()
    record(1, "nginx -t 配置语法通过", code == 0,
           "exit code == 0", {"exit": code, "output": out.splitlines()[-2:]})
    return code == 0


# ---------------- 2. nginx -T 生效值 ----------------
EXPECT = [
    ("hn_sio", "limit_req_zone $binary_remote_addr zone=hn_sio:10m rate=30r/m"),
    ("hn_login", "limit_req_zone $binary_remote_addr zone=hn_login:10m rate=20r/m"),
    ("hn_upload", "limit_req_zone $binary_remote_addr zone=hn_upload:10m rate=30r/m"),
    ("hn_api", "limit_req_zone $binary_remote_addr zone=hn_api:10m rate=60r/m"),
    ("hn_conn", "limit_conn_zone $binary_remote_addr zone=hn_conn:10m"),
    ("sio limit_conn 6", "limit_conn hn_conn 6;"),
    ("sio burst 20", "limit_req zone=hn_sio burst=20 nodelay;"),
    ("sio read timeout", "proxy_read_timeout 120s;"),
    ("api burst 150", "limit_req zone=hn_api burst=150 nodelay;"),
    ("api limit_conn 12", "limit_conn hn_conn 12;"),
    ("api body 2m", "client_max_body_size 2m;"),
    ("upload body 50m", "client_max_body_size 50m;"),
    ("profile body 3m", "client_max_body_size 3m;"),
    ("login body 64k", "client_max_body_size 64k;"),
    ("login burst 5", "limit_req zone=hn_login burst=5 nodelay;"),
    ("login limit_conn 3", "limit_conn hn_conn 3;"),
    ("profile limit_conn 4", "limit_conn hn_conn 4;"),
    ("root body 1m", "client_max_body_size 1m;"),
    ("root burst 20", "limit_req zone=hn_api burst=20 nodelay;"),
    ("client_header_timeout", "client_header_timeout 10s;"),
    ("client_body_timeout", "client_body_timeout 10s;"),
    ("send_timeout", "send_timeout 30s;"),
    ("keepalive_timeout", "keepalive_timeout 30s;"),
    ("reset_timedout_connection", "reset_timedout_connection on;"),
    ("limit_req_status 429", "limit_req_status 429;"),
    ("limit_conn_status 429", "limit_conn_status 429;"),
    ("server_tokens off", "server_tokens off;"),
]


def check_02():
    code, dump = sc.nginx_dump()
    missing = [n for n, s in EXPECT if s not in dump]
    # 行为侧：Server 响应头不带版本号
    import urllib.request
    try:
        r = urllib.request.urlopen(BASE + "/api/ping", timeout=5)
        server_hdr = r.headers.get("Server")
    except Exception as e:                                     # noqa: BLE001
        server_hdr = "error:%r" % e
    ok = (code == 0 and not missing and server_hdr == "nginx")
    record(2, "nginx -T 生效值核对（%d 条指令 + Server 头隐藏版本）" % len(EXPECT), ok,
           "27 条指令全部出现在 nginx -T；Server 头 == 'nginx'",
           {"exit": code, "missing": missing, "server_header": server_hdr,
            "dump_lines": len(dump.splitlines())},
           "缺失: %s" % missing if missing else "")
    return ok


# ---------------- 3/4. 16 条 WS：顺序 6 + 第 7 条起被挡 ----------------
def _burst_ws(n, token, source_ip="127.0.0.1", timeout=8.0):
    """n 条 WS 同时握手（保持成功连接打开）；返回 [{idx,status,open,ms,pkt}]。"""
    out = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        try:
            barrier.wait()
            t0 = time.perf_counter()
            ws, pkt, err = sc.ws_connect(source_ip=source_ip, token=token, timeout=timeout)
            ms = (time.perf_counter() - t0) * 1000
            if err:
                out[i] = {"idx": i + 1, "status": err.get("status") or err["kind"],
                          "open": False, "ms": round(ms, 1), "err": err["err"]}
            else:
                out[i] = {"idx": i + 1, "status": 101, "open": True,
                          "ms": round(ms, 1), "pkt": pkt[:40], "_ws": ws}
        except Exception as e:                                 # noqa: BLE001
            out[i] = {"idx": i + 1, "status": "error", "open": False,
                      "ms": 0, "err": repr(e)[:120]}

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout + 12)
    return out


def _close_all(conns):
    for c in conns:
        ws = c.get("_ws") if c else None
        if ws:
            try:
                ws.close()
            except Exception:                                  # noqa: BLE001
                pass
        c.pop("_ws", None) if c else None


def check_03_04():
    # --- 顺序形态：计划原文是「开 16 条 → 第 7 条起 429」---
    seq = []
    held = []
    for i in range(6):
        ws, pkt, err = sc.ws_connect(token=sc.STAGING_TOKEN, timeout=8,
                                     source_ip="127.0.0.1")
        seq.append({"n": i + 1, "status": 101 if ws else (err or {}).get("status"),
                    "pkt": (pkt or "")[:24]})
        if ws:
            held.append(ws)
    ok3 = all(r["status"] == 101 for r in seq) and len(held) == 6
    seq_tail = []
    for i in range(10):
        ws, pkt, err = sc.ws_connect(token=sc.STAGING_TOKEN, timeout=8,
                                     source_ip="127.0.0.1")
        seq_tail.append({"n": 7 + i, "status": 101 if ws else (err or {}).get("status")})
        if ws:                    # 不应发生；万一发生也记下来（会证明 limit_conn 没生效）
            held.append(ws)
    ok4_seq = all(r["status"] == 429 for r in seq_tail) and all(
        r["status"] != 101 for r in seq_tail)

    record(3, "开 16 条 WS（顺序）：第 1–6 条均 101 并收到 Engine.IO OPEN 包", ok3,
           "6/6 建连成功、pkt 以 '0{' 开头",
           {"seq": seq, "held": len(held),
            "access": access_lines_matching(
                lambda ln: "/socket.io/" in ln and "status=101" in ln, 3)})

    record(4, "第 7–16 条被边缘拒绝（429，limit_conn 6）", ok4_seq,
           "第 7..16 次全部 status=429（不得有任何一条建成）",
           {"seq_tail": seq_tail,
            "access_429": access_lines_matching(
                lambda ln: "/socket.io/" in ln and "status=429" in ln, 4),
            "nginx_err": nginx_error_tail(3),
            "conn_zone": "limit_conn hn_conn 6（每 IP 并发连接）"},
           "拒绝状态分布 %s" % [r["status"] for r in seq_tail] if not ok4_seq else "")

    # --- 饱和期间 HTTP 可用性（6 条仍开着）---
    lats, codes_ping = [], {}
    for _ in range(20):
        st, _b, ms = sc.http_get(BASE + "/api/ping", timeout=5)
        codes_ping[str(st)] = codes_ping.get(str(st), 0) + 1
        if st == 200:
            lats.append(ms)
    ok5 = codes_ping.get("200", 0) == 20
    record(5, "饱和期间 /api/ping 仍 200（16 条 WS 压不住 HTTP）", ok5,
           "20/20 == 200", {"status": codes_ping, "held_ws": len(held)})
    p95 = sc.pct(lats, 95)
    ok6 = ok5 and p95 is not None and p95 < 100
    record(6, "饱和期间 /api/ping p95 < 100 ms", ok6, "p95 < 100 ms（计划 §11.2）",
           {"p95_ms": p95, "p50_ms": sc.pct(lats, 50),
            "max_ms": round(max(lats), 2) if lats else None, "n": len(lats)})

    # --- 释放后恢复（"退避/恢复"观察）---
    for ws in held:
        try:
            ws.close()
        except Exception:                                      # noqa: BLE001
            pass
    time.sleep(1.5)
    ws, pkt, err = sc.ws_connect(token=sc.STAGING_TOKEN, timeout=8, source_ip="127.0.0.1")
    recovered = bool(ws)
    if ws:
        ws.close()
    RESULTS.append({"id": "3a", "title": "（附加观察）关闭 6 条后立刻重连 → 101（limit_conn 计数已释放）",
                    "pass": recovered, "criteria": "恢复建连",
                    "evidence": {"recovered": recovered, "err": err}})
    log("   释放后重连: %s" % ("101 成功" if recovered else str(err)))

    # --- 并发形态：16 条同时打，只能是 6 建立 / 10 拒绝 ---
    log("16 条**并发** WS（认证设备形态，单源 IP 127.0.0.2）")
    time.sleep(3.0)   # 等上一步的 6 条在 nginx 侧完全释放（否则会少一个 conn 名额）
    conns = _burst_ws(16, sc.STAGING_TOKEN, source_ip="127.0.0.2")
    est = [c for c in conns if c and c["open"]]
    rej = [c for c in conns if c and not c["open"]]
    codes = {}
    for c in rej:
        codes[c["status"]] = codes.get(c["status"], 0) + 1
    table = [{k: v for k, v in (c or {}).items() if k != "_ws"} for c in conns]
    concurrent_ok = len(est) == 6 and len(rej) == 10 and all(s == 429 for s in codes)
    RESULTS.append({"id": "4b", "title": "（附加观察）16 条并发：恰 6 条建立 + 10 条 429",
                    "pass": concurrent_ok, "criteria": "6 建立 / 10 拒绝且拒绝全为 429",
                    "evidence": {"established": len(est), "rejected": len(rej),
                                 "reject_status": codes, "table": table}})
    log("   并发 16 条: 建立 %d / 拒绝 %s" % (len(est), codes))
    _close_all(conns)
    time.sleep(1.2)

    # --- 无 token 形态：一把都建不起来（S0-02 门 + 边缘）---
    conns2 = _burst_ws(16, None, source_ip="127.0.0.3")
    anon_codes = {}
    for c in (conns2 or []):
        if c:
            anon_codes[c["status"]] = anon_codes.get(c["status"], 0) + 1
    anon_tbl = [{k: v for k, v in (c or {}).items() if k != "_ws"} for c in conns2]
    anon_ok = len(anon_codes) > 0 and all(s in (403, 429) for s in anon_codes)
    _close_all(conns2)
    log("   无 token 16 条并发: %s" % anon_codes)
    RESULTS.append({"id": "4a", "title": "（附加观察）无 token 16 条：全部 403/429，无一建成",
                    "pass": anon_ok, "criteria": "全部 ∈ {403,429}",
                    "evidence": {"status": anon_codes, "table": anon_tbl}})
    time.sleep(1.0)
    return ok3 and ok4_seq


# ---------------- 8. 6 个 Word 同时轮询（72 r/m，持续 60s） ----------------
def check_08():
    """6 个待验证 Word × 每 5s 轮询 1 次 × 12 轮 = 72 r/m，持续 60s。

    计划判据：**不出现 503**（`/api/` 的 burst 必须保持 150，不能被压到 30 ——
    30 的话 72 r/m 的正常轮询会被打成 429/503）。
    用独立源 IP（127.0.0.5），避免前面用例把这个 IP 的桶吃掉。
    """
    ip = "127.0.0.5"
    codes, lats = {}, []
    lock = threading.Lock()
    STOP = time.time() + 60.0

    def worker(_i):
        n = 0
        while time.time() < STOP and n < 12:
            st, _b, ms = sc.http_get("/api/ping", timeout=5, source_ip=ip)
            with lock:
                codes[str(st)] = codes.get(str(st), 0) + 1
                if st == 200:
                    lats.append(ms)
            n += 1
            time.sleep(5.0)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join(90)
    dt = time.time() - t0
    total = sum(codes.values())
    rpm = total / max(dt, .001) * 60
    ok = (codes.get("200", 0) == total and total >= 60
          and "503" not in codes and "429" not in codes)
    record(8, "6 个待验证 Word 同时轮询（%d 次 / %.0fs ≈ %.0f r/m）无 503/429" % (total, dt, rpm),
           ok, "全部 200；无 503；无 429（burst 150 未被压到 30）",
           {"status": codes, "req": total, "seconds": round(dt, 1), "rpm": round(rpm, 1),
            "p95_ms": sc.pct(lats, 95), "source_ip": ip})
    return ok


# ---------------- 9. /api/ >2MB → 413 ----------------
def check_09():
    body = b"x" * (3 * 1024 * 1024)
    st, txt, ms = sc.http_post("/api/ping", body, source_ip="127.0.0.14",
                              headers={"Content-Type": "application/json"}, timeout=30)
    ok = st == 413
    record(9, "/api/ 普通接口 3 MB body → 413（client_max_body_size 2m）", ok, "status == 413",
           {"status": st, "ms": round(ms, 1), "body": str(txt)[:80], "source_ip": "127.0.0.14"})
    time.sleep(0.5)
    return ok


# ---------------- 10/11. /api/upload 50MB vs 53MB ----------------
def _upload_body(size, boundary="----stagingS0"):
    """构造一个合法的 multipart 请求体（单文件字段，头部真实、内容填 0）。"""
    head = ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"s0_probe.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n" % boundary).encode()
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    pad = size - len(head) - len(tail)
    return head + b"0" * max(pad, 0) + tail, "multipart/form-data; boundary=%s" % boundary


def check_10():
    body, ctype = _upload_body(50 * 1024 * 1024)
    st, txt, ms = sc.http_post(
        "/api/upload", body, source_ip="127.0.0.8",
        headers={"Content-Type": ctype, "X-Printer-Token": sc.STAGING_TOKEN}, timeout=180)
    temp_left = temp_dir_listing()
    ok = st != 413
    record(10, "/api/upload 50 MB body 未被边缘 413（50m 上限维持、不放宽）", ok,
           "status != 413（边缘放行；后端返回值另记）",
           {"status": st, "ms": round(ms, 1), "body": str(txt)[:120],
            "client_body_temp_after": temp_left})
    time.sleep(0.5)
    return ok


def check_11():
    body, ctype = _upload_body(53 * 1024 * 1024)
    t0 = time.perf_counter()
    st, txt, ms = sc.http_post(
        "/api/upload", body, source_ip="127.0.0.8",
        headers={"Content-Type": ctype, "X-Printer-Token": sc.STAGING_TOKEN}, timeout=180)
    time.sleep(0.8)
    left = temp_dir_listing()
    # 413 之前/之后都不应留下可观测的请求体临时文件
    ok = st == 413 and not left
    record(11, "/api/upload 53 MB → nginx 413 且 client_body_temp 无残留", ok,
           "status == 413 且 client_body_temp 空",
           {"status": st, "ms": round(ms, 1), "body": str(txt)[:80],
            "client_body_temp_after": left,
            "nginx_err_tail": nginx_error_tail(3)})
    return ok


# ---------------- 12/13. 登录入口独立桶 ----------------
def _login_probe(n, route, payload, source_ip):
    out = []
    for i in range(n):
        st, txt, ms = sc.http_post(route, json.dumps(payload).encode(), source_ip=source_ip,
                                   headers={"Content-Type": "application/json"}, timeout=20)
        out.append({"n": i + 1, "status": st, "ms": round(ms, 1), "body": str(txt)[:60]})
    return out


def _rate_check(cid, title, route, payload, source_ip):
    rows = _login_probe(8, route, payload, source_ip)
    first_429 = next((r["n"] for r in rows if r["status"] == 429), None)
    # 判据（计划原文：第 7 次起 429）：前 5 次必须被放行（非 429），首次 429 出现在第 6~7 次
    allowed_first5 = all(r["status"] != 429 for r in rows[:5])
    ok = allowed_first5 and first_429 in (6, 7)
    record(cid, title, ok,
           "前 5 次非 429；首次 429 在 6~7（rate=20r/m burst=5）",
           {"first_429_at": first_429, "rows": rows,
            "access": access_lines_matching(lambda ln: route in ln, 4)})
    return ok


def check_12():
    ok = _rate_check(12, "/api/login 第 6~7 次起 429（20r/m burst 5 + limit_conn 3）",
                     "/api/login", {"code": "staging-code", "nickname": "s0"},
                     source_ip="127.0.0.6")
    time.sleep(1.0)
    return ok


def check_13():
    ok = _rate_check(13, "/api/device_login 第 6~7 次起 429（独立桶）",
                     "/api/device_login", {"device_id": "staging-device-0001"},
                     source_ip="127.0.0.7")
    time.sleep(1.0)
    return ok


# ---------------- 14. /api/profile 3MB ----------------
def check_14():
    body, ctype = _upload_body(4 * 1024 * 1024, boundary="----stagingProfile")
    st, txt, ms = sc.http_post("/api/profile", body, source_ip="127.0.0.15",
                              headers={"Content-Type": ctype}, timeout=60)
    ok = st == 413
    record(14, "/api/profile 4 MB → 413（client_max_body_size 3m）", ok, "status == 413",
           {"status": st, "ms": round(ms, 2), "body": str(txt)[:80]})
    time.sleep(0.5)
    return ok


# ---------------- 15. / 的 404 洪水 ----------------
def check_15():
    rows = []
    for i in range(60):
        st, _b, ms = sc.http_get("/nope-%d-%d" % (int(time.time()), i), timeout=5,
                                 source_ip="127.0.0.9")
        rows.append(st)
        if st == 429:
            break
    first_429 = next((i + 1 for i, s in enumerate(rows) if s == 429), None)
    ok = (rows[0] in (404, 200)) and first_429 is not None and first_429 <= 25
    record(15, "/ 的 404 洪水被限速（hn_api 60r/m burst 20 → 429）", ok,
           "首个请求非 429；60 次内出现 429（≤25 次）",
           {"first_429_at": first_429, "status_seq_head": rows[:26],
            "allowed": sum(1 for s in rows if s != 429)})
    return ok


# ---------------- 16. 慢连接（Slowloris） ----------------
def check_16():
    s = socket.create_connection(("127.0.0.1", sc.EDGE_PORT), timeout=20)
    t0 = time.perf_counter()
    try:
        # 只发请求行的前半段，不发结束的空行 —— 慢连接典型形态
        s.sendall(b"GET /api/ping HTTP/1.1\r\nHost: staging\r\nX-Slow: ")
        s.settimeout(20)
        closed = False
        resp = b""
        try:
            while time.perf_counter() - t0 < 18:
                chunk = s.recv(4096)
                if not chunk:
                    closed = True
                    break
                resp += chunk
        except (socket.timeout, ConnectionResetError, ConnectionAbortedError):
            closed = True
        dt = time.perf_counter() - t0
    finally:
        try:
            s.close()
        except Exception:                                      # noqa: BLE001
            pass
    # nginx 在 10s 后 408 并关闭（reset_timedout_connection on）
    ok = closed and 8.0 <= dt <= 13.0
    record(16, "慢连接 10s 超时断开（client_header_timeout 10s，不占住后端）", ok,
           "8s ≤ 关闭耗时 ≤ 13s（期望 ~10s）",
           {"elapsed_s": round(dt, 2), "closed": closed,
            "response_head": resp[:120].decode("latin-1", "replace"),
            "nginx_err_tail": nginx_error_tail(3)})
    return ok


# ---------------- 7. /socket.io/ 速率桶 ----------------
def check_07():
    """hn_sio: 30r/m（0.5 r/s）burst 20 nodelay —— 串行快速握手应在前 20 次左右后 429。

    串行 = 不触碰 limit_conn 6；只测请求速率桶。
    """
    rows = []
    for i in range(41):
        ws, pkt, err = sc.ws_connect(token=sc.STAGING_TOKEN, timeout=8,
                                     source_ip="127.0.0.10")
        if ws:
            rows.append({"n": i + 1, "status": 101})
            ws.close()
        else:
            rows.append({"n": i + 1, "status": err.get("status") if err else "err"})
    first_429 = next((r["n"] for r in rows if r["status"] == 429), None)
    ok = (rows[0]["status"] == 101 and first_429 is not None and 15 <= first_429 <= 25)
    record(7, "/socket.io/ 请求速率桶生效（30r/m burst 20；串行第 %s 次起 429）" % first_429, ok,
           "首条 101；首次 429 在 15~25 次（burst 20 量级）",
           {"first_429_at": first_429,
            "seq": [r["status"] for r in rows],
            "access": access_lines_matching(lambda ln: "/socket.io/" in ln, 6)})
    return ok


def main():
    log("staging edge = %s（先确认 staging_up.py 已跑）" % BASE)
    if not sc.port_open(sc.EDGE_PORT):
        sys.exit("edge 未监听，请先跑 tools/staging/staging_up.py")
    log("重启 nginx（清空 limit_req/limit_conn 共享 zone，保证桶是满的）")
    sc.restart_nginx()
    time.sleep(0.5)
    results = {}
    results[1] = check_01()
    results[2] = check_02()
    if not (results[1] and results[2]):
        log("配置层不通过，后续项仍继续跑（证据要留全）")
    check_03_04()
    results[3] = next(r for r in RESULTS if r["id"] == 3)["pass"]
    results[4] = next(r for r in RESULTS if r["id"] == 4)["pass"]
    results[8] = check_08()
    results[9] = check_09()
    results[10] = check_10()
    results[11] = check_11()
    results[12] = check_12()
    results[13] = check_13()
    results[14] = check_14()
    results[15] = check_15()
    results[16] = check_16()
    results[7] = check_07()

    passed = sum(1 for r in RESULTS if r.get("pass"))
    total_main = 16
    main_pass = sum(1 for i in range(1, 17)
                    if next((r for r in RESULTS if r["id"] == i), {}).get("pass"))
    payload = {
        "task": "S0-01 staging 16 条 WS 饱和复现",
        "plan": "docs/安全加固修复计划_2026-10-23.md §S0-01（第 250–261 行）+ §11.2（第 557 行）",
        "staging": sc.status(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(time.time() - T0, 1),
        "summary": {"main_16_pass": main_pass, "main_16_total": total_main,
                    "aux_rows_pass": passed - main_pass, "rows_total": len(RESULTS)},
        "checks": RESULTS,
        "nginx_access_sample": nginx_access_tail(40),
    }
    sc.save_result("s0_01", payload)
    print("\n==== S0-01 汇总：16 条中 %d 条 PASS ====" % main_pass)
    for r in RESULTS:
        print("  %-4s %-5s %s" % (r["id"], "PASS" if r.get("pass") else "FAIL", r["title"]))
    return 0 if main_pass == 16 else 1


if __name__ == "__main__":
    sys.exit(main())
