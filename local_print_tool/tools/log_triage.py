"""Failure-log triage for the local print tool, using Jev for the semantic judgment.

Why this exists
---------------
local_tool.log is written at INFO level only — the sample log has 1846 lines and not one
WARNING or ERROR. Severity is not in the level field; it is in the message text. A keyword
scan over that text both misses things and invents them:

    ========== 打印完毕：成功 1，失败 0 ==========   ← contains 失败, reports success
    📦 缓存命中: 示例文档.pdf → 0 页                ← a real fault, contains no fault word

Both of those are live in the corpus, and the keyword scan scores 0.80 / 0.80 against the
hand labels where this scores 1.00 / 0.80-1.00. Run `evaluate.py` to reproduce.

The corpus
----------
One event per distinct log line, with identity masked and measurements kept:

    📋 已回报本机日志（云端日志收集, 0 字节）        ← its own event, and it is the odd one
    📋 已回报本机日志（云端日志收集, 4810 字节）     ← not merged with the line above
    📦 已清理缓存: <MD5>...                          ← eight v1 events, one event here

That distinction is the whole design. An order id, a device name or a file name differs
because it names an instance, so it is masked and the occurrences gather; a count, a page
number or a byte size differs because something is wrong, so it is kept. The version this
replaces blanked every digit run, which did the opposite in both directions: it hid the
0-byte upload behind a sibling reading 4810, and it split `已清理缓存` into eight events.
The rules are pinned by tools/verify_redaction.py.

Jev is not perfectly deterministic. Across three identical runs over this corpus the flagged
set moved by one event and recall ranged 0.80-1.00; precision held at 1.00 — zero false
positives, on all three. The one event crossing the cut is `缓存命中 … → 0 页`, the genuinely
ambiguous "cached page count is zero" line, which lands at 0.39 or 0.45 depending on the run
and so is either flagged or printed in the uncertain tail. Read the score as a ranking, and
treat events near the cut as "worth a look" rather than as decided.

How it works
------------
    unit       one distinct log event (parsed, masked, clustered)
    state      the line, plus occurrence count and time span that code computes
    questions  Noul   needs_attention  - must an operator act?
               Choice category         - which subsystem is implicated
               Score  severity         - how bad, 4 concrete levels
    code       owns masking, the threshold, aggregation and the report

Jev only judges. Every number in the output comes from code arithmetic over its answers.

Usage
-----
    python tools/log_triage.py                    # triage %APPDATA%\\HN打印工具\\logs\\local_tool.log
    python tools/log_triage.py --log some.log
    python tools/log_triage.py --dry-run          # build the corpus and check the payload; no API call
    python tools/log_triage.py --fixture          # judge the committed corpus instead
    python tools/log_triage.py --write-fixture triage_fixture.jsonl   # rebuild the corpus
    python tools/log_triage.py --fixture --no-cache --out out/run1.jsonl

Needs TYPESAFE_API_KEY in the environment or in the repository-root .env (git-ignored).
Judgments and the request cache are written under tools/out/, also git-ignored.

Nothing is sent to the model until the payload has been checked against the leak rules, and
a payload that trips one is refused rather than sent — the previous version printed a warning
beside it and sent it anyway, which is how customer file names and an employee name left the
machine. `--dry-run` reports the same check without spending a request.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                     # vendored jev_client.py
sys.path.insert(0, str(HERE.parent))              # paths.py from the tool root

from jev_client import JevClient                  # noqa: E402

# The key lives in the repository-root .env (git-ignored), not next to this file, and the
# tool is normally run from local_print_tool/, so name the locations instead of relying on cwd.
ENV_FILES = (HERE.parent.parent / ".env", HERE / ".env", ".env")

LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$")

# Decision thresholds, re-measured on the committed corpus (run tools/evaluate.py to
# reproduce). Over three runs the labeled faults scored 0.39-0.86 and the routine events
# 0.02-0.36, so the gap is narrow and the cut has to sit inside it. 0.40 does: precision
# 1.00 on all three runs, recall 0.80-1.00. The cut is not sharp — 0.35 already lets a
# routine event through on some runs (precision 0.83), and 0.50 reaches precision 1.00 but
# drops recall to a stable 0.80 by cutting the 0-page cache fault entirely. The ordering is
# stable and the cut is provisional, which is what IGNORE_BELOW is for: the band below the
# cut gets reported as "worth a look" rather than quietly decided.
FLAG_AT = 0.40
IGNORE_BELOW = 0.25

# The domain block Jev needs. It knows nothing about this system, and unrelated context
# lowers accuracy, so this stays short and stays in its own state field.
CONTEXT = (
    "Log lines from a Windows desktop print tool that converts documents to PDF and "
    "prints them silently, and that also receives print orders from a cloud service over "
    "SocketIO. The operator runs the shop; a job that does not print is a customer-facing "
    "failure. Startup, tab management, cache cleanup and connection retries are routine "
    "and self-healing. Lines are Chinese; the leading emoji is decoration, not severity."
)

QUESTIONS = {
    "needs_attention": {
        "type": "noul",
        "instructions": {
            "question": "Does `event.line` report a condition an operator must act on to keep the tool or the shop working?",
            "focus": "Judge the condition this single line reports, not what earlier lines may have caused.",
            "exclude": "Routine progress, startup, deliberate user operations, and automatic retries that are expected to resolve on their own.",
        },
        "criteria": {
            "true": "The line reports a failure, a degraded or stuck state, or data that is wrong (a job that will not print, an unreachable server after retries, a count or page number that cannot be right). Someone must investigate or intervene.",
            "false": "The line reports normal progress, a successful operation, a deliberate user action, a setting change, or an automatic retry/cleanup that is expected and self-healing.",
        },
    },
    "category": {
        "type": "choice",
        "instructions": "Which part of the system does `event.line` concern?",
        "criteria": {
            "connectivity": {"what": "Reaching the cloud server: connecting, reconnecting, timeouts, connection refused", "not_for": "Cloud order bookkeeping that happens over a working connection"},
            "printing": {"what": "Producing pages: print start, per-sheet success, print batch summary", "not_for": "Preparing a file before it prints"},
            "file_handling": {"what": "Staged file copies, format conversion to PDF, the PDF/page cache", "not_for": "Downloading a cloud task's file"},
            "cloud_order": {"what": "Cloud order and task bookkeeping: receiving, downloading, claiming, reporting status", "not_for": "Whether the server is reachable"},
            "update": {"what": "The self-update flow: checking, downloading, installing a new version"},
            "config": {"what": "Settings, pricing sync, theme, saved configuration"},
            "lifecycle": {"what": "Start, exit, duplicate-launch handling, tab add/close/clear, printer list refresh"},
            "unknown": "The line does not make its subsystem determinable",
        },
    },
    "severity": {
        "type": "score",
        "instructions": "How much attention does `event.line` warrant on its own?",
        "criteria": [
            "Nothing to do; routine progress or a successful operation",
            "Worth noting in passing; advisory or informational only",
            "Should be looked at soon; a degraded condition or suspect data that is not yet blocking work",
            "Act now; printing or cloud order intake is broken, or data is wrong in a way that affects a customer",
        ],
    },
}


# --------------------------------------------------------------------------- corpus

# --------------------------------------------------------------------------- redaction
#
# Identity out, measurements kept.
#
# What this replaces blanked every digit run. That is the first thing a person writes and it
# is wrong in both directions. `成功 1，失败 0` and `成功 3，失败 2` collapsed into a single
# event whose text was whichever occurrence came first — so a failed batch sat behind a
# passing one, which is the one failure this tool exists to prevent. Meanwhile a bare MD5
# left in the text split a single event into eight.
#
# The two kinds of number are not alike. A count, a page number and a byte size differ
# because something is wrong, or because two documents differ: keep them. An order id, a
# device name and a file name differ because they name an instance: mask them, and the
# occurrences that share a template gather into one event, which is the aggregation the
# severity question reads. Every rule is anchored on the literal that introduces its field,
# so a rule cannot reach a value it was not meant for, and what each rule does to a real
# line is pinned in tools/verify_redaction.py.

FILE_EXTENSIONS = "docx?|pdf|jpe?g|png|bmp|gif|tiff?|txt|csv|md|xlsx?|pptx?|log"

REDACTIONS: tuple = (
    # A query string carries the printer token, the client id, the machine name, and a
    # cache-busting timestamp that would otherwise make every reconnect its own event. The
    # path is what triage reads, so the whole query goes: nothing after `?` survives to leak.
    (re.compile(r"(/[\w.\-]+)/?\?[^\s)\"']*"), r"\1"),
    (re.compile(r"([?&](?:token|client_id|device_name)=)[^&\s)\"']+"), r"\1<REDACTED>"),
    (re.compile(r"https?://\S+"), "<URL>"),
    (re.compile(r"wss://\S+"), "<URL>"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"), r"C:\\Users\\<USER>"),
    (re.compile(r"(当前接单设备[:：]\s*)[^（(\s]+"), r"\1<DEVICE>"),
    (re.compile(r"(所有者[:：]\s*)[^）)\s]+"), r"\1<OWNER>"),
    (re.compile(r"\bHN\d+-\d+\b"), "<ORDER>"),
    # Keep the extension: a format is not an identity, and it is what tells the reader
    # whether the line concerns a Word document or a scan.
    (re.compile(r"\S*?\.(%s)\b" % FILE_EXTENSIONS), r"<FILE>.\1"),
    (re.compile(r"\b[0-9a-fA-F]{6,}\.\.\."), "<MD5>..."),
    (re.compile(r"\b[0-9a-fA-F]{32}\b"), "<MD5>"),
    # Which rung of a retry ladder a line reports is positional, not diagnostic: the
    # escalation is what matters, and `occurrences` plus the time span already carry it,
    # while the seconds on their own shattered one event into eighteen.
    (re.compile(r"\d+s( 后重连)"), r"<N>s\1"),
    # A release number names an instance. Keeping it split two upgrade lines per release.
    (re.compile(r"\bv\d+(?:\.\d+)+\b"), "v<VER>"),
    # A tab index names an instance; the file count beside it is a measurement and stays.
    (re.compile(r"(标签页\s*)\d+"), r"\1<N>"),
    (re.compile(r"#\d+"), "#<N>"),
)

# Anything that must not leave the machine, checked against the exact payload before it is
# sent. A dirty payload is refused and the run stops — never sent with a warning printed
# beside it, which is how the previous version leaked file names in the first place.
LEAKS: tuple = (
    (r"(?i)\btoken=(?!<REDACTED>)", "printer token"),
    (r"(?i)\bclient_id=(?!<REDACTED>)", "client id"),
    (r"(?i)\bdevice_name=(?!<REDACTED>)", "device name"),
    (r"https?://|wss://", "url"),
    (r"[A-Za-z]:\\Users\\(?!<USER>)", "windows user path"),
    (r"(所有者[:：])(?!\s*<OWNER>)", "owner name"),
    (r"(当前接单设备[:：])(?!\s*<DEVICE>)", "device name"),
    (r"\bHN\d+-\d+\b", "order number"),
    (r"#\d+", "task id"),
    (r"\b[0-9a-fA-F]{6,}\.\.\.", "hash prefix"),
    (r"\b[0-9a-fA-F]{32}\b", "hash"),
    (r"/[\w.\-]+\?[^\s)\"']", "query string"),
)

# The fixture is committed and read by evaluate.py; it must never carry a raw line. Fields
# are chosen by allow-list, not filtered out, so a field added later cannot leak by default.
FIXTURE_FIELDS = ("id", "line", "level", "count", "first", "last")

LEVEL_RANK = {"CRITICAL": 50, "ERROR": 40, "WARNING": 30, "WARN": 30, "INFO": 20, "DEBUG": 10}


def redact(text: str) -> str:
    """Mask what identifies an instance, keep what measures it."""
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def normalize(text: str) -> str:
    """The cluster key: the line with its identity masked and its whitespace settled."""
    return re.sub(r"\s+", " ", redact(text)).strip()


def leaks(text: str) -> list[str]:
    """Names of the leak rules `text` trips. Empty means the payload is safe to send."""
    found = [name for pattern, name in LEAKS if re.search(pattern, text)]
    if re.search(r"\.(%s)\b" % FILE_EXTENSIONS, re.sub(r"<FILE>\.[a-z]+", "", text)):
        found.append("file name")
    return found


def public(ev: dict) -> dict:
    """The serializable view of an event. `_members` stays in memory and out of the fixture."""
    return {k: ev[k] for k in FIXTURE_FIELDS}


def build_corpus(lines: list[str]) -> tuple[list[dict], list[str]]:
    """Parse, redact and cluster log lines into judgeable events.

    Returns (events, unparsed). Unparsed lines are counted and handed back rather than
    dropped, so a log-format change shows up as a shrinking corpus instead of a clean
    bill of health — the failure mode of a triage tool that has quietly stopped reading.
    """
    groups: dict[str, dict] = {}
    unparsed: list[str] = []
    for raw in lines:
        m = LOG_LINE.match(raw)
        if not m:
            if raw.strip():
                unparsed.append(raw)
            continue
        ts, level, msg = m.groups()
        line = normalize(msg)
        if not line:
            continue
        ev = groups.get(line)
        if ev is None:
            ev = groups[line] = {"line": line, "level": level, "count": 0,
                                 "first": ts, "last": ts, "_members": []}
        ev["count"] += 1
        ev["last"] = ts
        # The worst variant speaks for the cluster: if any occurrence was logged at a
        # higher level, that is the one the judge should see.
        if LEVEL_RANK.get(level.upper(), 0) > LEVEL_RANK.get(ev["level"].upper(), 0):
            ev["level"] = level
        if len(ev["_members"]) < 3 and ts not in [t for t, _, _ in ev["_members"]]:
            ev["_members"].append((ts, level, raw))
    events = list(groups.values())
    for n, ev in enumerate(events, 1):
        ev["id"] = "L%03d" % n
    return events, unparsed


def load_events(args) -> tuple[list[dict], list[str]]:
    if args.fixture:
        path = HERE / "triage_fixture.jsonl"
        events = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        return (events[:args.limit] if args.limit else events), []
    if args.log:
        path = Path(args.log)
    else:
        import paths                                      # only needed for the live-log path
        path = Path(paths.logs_dir()) / "local_tool.log"
    if not path.is_file():
        raise SystemExit("log not found: %s\n(point --log at a file, or use --fixture)" % path)
    events, unparsed = build_corpus(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return (events[:args.limit] if args.limit else events), unparsed


# --------------------------------------------------------------------------- judging

def build_request(ev: dict) -> dict:
    """One request per event. Code computes the facts; Jev judges the fuzzy part."""
    seen = ev["first"][:10]
    if ev["last"][:10] != ev["first"][:10]:
        seen = "%s to %s" % (ev["first"][:10], ev["last"][:10])
    state = {
        "system": CONTEXT,
        "event": {
            "id": ev["id"],
            "line": ev["line"],
            "level": ev["level"],
            "occurrences": ev["count"],
            "seen": seen,
        },
    }
    return {"state": state, "questions": QUESTIONS}


def report(rows: list[dict], flag_at: float, unparsed: list[str] = ()) -> None:
    ok = [r for r in rows if "error" not in r]
    failed = [r for r in rows if "error" in r]
    flagged = [r for r in ok if r["attention"] >= flag_at]
    uncertain = [r for r in ok if IGNORE_BELOW <= r["attention"] < flag_at]

    print("\n=== flagged (P(needs action) >= %.2f): %d of %d events ===" % (flag_at, len(flagged), len(ok)))
    for r in sorted(flagged, key=lambda r: (-r["severity"], -r["attention"])):
        print("  %s  sev=%.2f  att=%.2f  %-13s [%dx]  %s"
              % (r["id"], r["severity"], r["attention"], r["category"], r["count"], r["line"][:88]))

    if uncertain:
        print("\n--- uncertain tail (%.2f-%.2f): needs more context, do not auto-flag ---"
              % (IGNORE_BELOW, flag_at))
        for r in sorted(uncertain, key=lambda r: -r["attention"]):
            print("  %s  att=%.2f  %-13s [%dx]  %s"
                  % (r["id"], r["attention"], r["category"], r["count"], r["line"][:80]))

    print("\n=== what dominates the log (all events) ===")
    for cat, n in Counter(r["category"] for r in ok).most_common():
        print("  %-13s %3d" % (cat, n))

    if failed:
        print("\n=== not judged (%d) — a service failure, distinct from a negative answer ===" % len(failed))
        for r in failed:
            print("  %s  %s" % (r["id"], r["error"]))

    if unparsed:
        print("\n=== %d lines did not match the log format and were not judged ===" % len(unparsed))
        for raw in unparsed[:5]:
            print("  %s" % raw[:100])
        if len(unparsed) > 5:
            print("  ... and %d more" % (len(unparsed) - 5))

    tokens = sum(r.get("tokens", 0) for r in ok)
    print("\ntotal input tokens: %d  (~$%.5f at $0.042/M)" % (tokens, tokens * 0.042 / 1e6))


def preflight(bodies: list[dict], events: list[dict]) -> list[str]:
    """Check the exact payload against the leak rules. Returns a list of complaints."""
    complaints = []
    for ev, body in zip(events, bodies):
        for name in leaks(json.dumps(body, ensure_ascii=False)):
            complaints.append("%s  %s: %s" % (ev["id"], name, ev["line"][:80]))
    return complaints


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", help="log file to triage (default: the tool's own APPDATA log)")
    ap.add_argument("--fixture", action="store_true", help="judge the committed fixture instead of a local log")
    ap.add_argument("--write-fixture", metavar="PATH",
                    help="write the corpus built from the log to PATH and stop (allow-listed fields only)")
    ap.add_argument("--out", default="out/judgments.jsonl", help="where to write judgments")
    ap.add_argument("--threshold", type=float, default=FLAG_AT, help="flag at or above this P(needs action)")
    ap.add_argument("--limit", type=int, help="only the first N events")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the request cache; required to measure run-to-run spread")
    ap.add_argument("--show-members", type=int, default=0, metavar="N",
                    help="with --dry-run, also print N raw occurrences per event for eyeballing")
    ap.add_argument("--dry-run", action="store_true", help="build the corpus and print it; no API call")
    args = ap.parse_args()

    events, unparsed = load_events(args)
    print("events: %d  (total occurrences: %d)"
          % (len(events), sum(e["count"] for e in events)))

    if args.write_fixture:
        path = Path(args.write_fixture)
        path = path if path.is_absolute() else HERE / path
        with path.open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(public(ev), ensure_ascii=False) + "\n")
        print("wrote %d events to %s" % (len(events), path))
        return 0

    bodies = [build_request(e) for e in events]

    if args.dry_run:
        for ev in events:
            print("  %s [%3dx] %s" % (ev["id"], ev["count"], ev["line"][:110]))
            for ts, _level, raw in ev.get("_members", [])[:args.show_members]:
                if raw.strip() != ev["line"]:
                    print("        raw %s  %s" % (ts, raw[:96]))
        if unparsed:
            print("\n%d lines did not match the log format and were not judged:" % len(unparsed))
            for raw in unparsed[:10]:
                print("  %s" % raw[:100])
        complaints = preflight(bodies, events)
        print("\nredaction preflight over the exact payload: %d complaint(s)" % len(complaints))
        for c in complaints[:20]:
            print("  LEAK  %s" % c)
        print("  %s" % ("CLEAN — safe to send" if not complaints else "REFUSING to send this corpus"))
        return 1 if complaints else 0

    complaints = preflight(bodies, events)
    if complaints:
        print("refusing to send: %d leak complaint(s) in the payload" % len(complaints), file=sys.stderr)
        for c in complaints[:20]:
            print("  %s" % c, file=sys.stderr)
        return 2

    out = Path(args.out)
    out = out if out.is_absolute() else HERE / out
    out.parent.mkdir(parents=True, exist_ok=True)

    client = JevClient(env_files=ENV_FILES,
                       cache_dir=None if args.no_cache else out.parent / "cache")
    t0 = time.time()
    results = client.ask_many(bodies, concurrency=args.concurrency)
    elapsed = time.time() - t0

    rows = []
    for ev, res in zip(events, results):
        if "error" in res:
            rows.append({**public(ev), "error": res["error"], "status": res.get("status")})
            continue
        a = res["answers"]
        rows.append({**public(ev),
                     "attention": a["needs_attention"]["noul"],
                     "category": a["category"]["choice"],
                     "category_confidence": a["category"]["confidence"],
                     "severity": a["severity"]["score"],
                     "tokens": res.get("usage", {}).get("input_tokens", 0)})

    report(rows, args.threshold, unparsed)
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    judged = sum(1 for r in rows if "error" not in r)
    print("judged %d events in %.1fs (concurrency %d) -> %s" % (judged, elapsed, args.concurrency, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
