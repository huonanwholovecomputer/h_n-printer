"""Score triage runs against the hand labels, and against the keyword scan it replaces.

    python tools/log_triage.py --fixture --no-cache --out out/run1.jsonl
    python tools/log_triage.py --fixture --no-cache --out out/run2.jsonl
    python tools/log_triage.py --fixture --no-cache --out out/run3.jsonl
    python tools/evaluate.py out/run1.jsonl out/run2.jsonl out/run3.jsonl

Jev is not deterministic: identical payloads move, and events near the cut cross it. A single
run therefore reports a sample, not a rate, and this scores every run it is given and prints
the interval. `--no-cache` matters — the request cache would return the same answer three
times and manufacture a spread of zero.

The labels come from tools/labels.json, whose provenance is per event: most are inherited
from the v1 corpus, eleven were judged by hand when the rebuild separated them from a
sibling, and relabel.py refuses to invent the rest. They are hand labels by one rater, not
incident-verified ground truth, and only a handful are positive — so each event moves recall
by a lot. Treat the ranking and the false-positive behaviour as the finding, and the exact
cut point as provisional.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# A scan for fault words, which is what a person writes first.
KEYWORDS = ["失败", "错误", "无法", "拒绝", "异常", "超时", "不存在", "损坏", "丢失"]
THRESHOLDS = [0.90, 0.80, 0.70, 0.60, 0.50, 0.45, 0.40, 0.35, 0.30, 0.20, 0.10]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def stats(gold: dict, pred: dict) -> tuple:
    tp = sum(1 for i in gold if pred.get(i) and gold[i])
    fp = sum(1 for i in gold if pred.get(i) and not gold[i])
    fn = sum(1 for i in gold if not pred.get(i) and gold[i])
    tn = sum(1 for i in gold if not pred.get(i) and not gold[i])
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else float("nan")
    return tp, fp, fn, tn, precision, recall, f1


def interval(values: list[float]) -> str:
    lo, hi = min(values), max(values)
    return "%.2f" % lo if lo == hi else "%.2f-%.2f" % (lo, hi)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("judgments", nargs="*", default=[],
                    help="one out/*.jsonl per run (the same corpus each); default out/judgments.jsonl")
    ap.add_argument("--threshold", type=float, default=0.40, help="cut point to score at")
    ap.add_argument("--min-precision", type=float, default=None,
                    help="fail unless every run reaches this precision at the cut")
    args = ap.parse_args()

    labels = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))
    text = {e["id"]: e["line"] for e in load_jsonl(HERE / "triage_fixture.jsonl")}
    gold_all = {k: v["needs_attention"] for k, v in labels.items()}

    def resolve(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else HERE / path

    runs = []
    for p in (args.judgments or ["out/judgments.jsonl"]):
        path = resolve(p)
        if not path.is_file():
            print("no judgments at %s — run: python tools/log_triage.py --fixture --no-cache "
                  "--out %s" % (path, p))
            return 2
        rows = [r for r in load_jsonl(path) if "error" not in r]
        errored = [r for r in load_jsonl(path) if "error" in r]
        if not rows:
            print("%s holds no usable judgments" % path)
            return 2
        runs.append((path.name, rows, errored))

    ids = {r["id"] for r in runs[0][1]}
    for name, rows, _ in runs:
        if {r["id"] for r in rows} != ids:
            print("%s does not cover the same events as %s; every run must judge one corpus"
                  % (name, runs[0][0]))
            return 2
    gold = {i: gold_all[i] for i in ids if i in gold_all}
    unlabeled = sorted(ids - set(gold))
    positives = sum(gold.values())

    print("runs: %d | labeled events: %d | labeled positive: %d | unlabeled: %d | scoring at >= %.2f\n"
          % (len(runs), len(gold), positives, len(unlabeled), args.threshold))
    if unlabeled:
        print("unlabeled events are not scored (run tools/relabel.py to see why): %s\n"
              % ", ".join(unlabeled))

    preds = {name: {r["id"]: r["attention"] >= args.threshold for r in rows}
             for name, rows, _ in runs}

    print("%-22s %4s %4s %4s %4s %10s %8s %6s" % ("method", "TP", "FP", "FN", "TN", "precision", "recall", "F1"))
    for name, rows, _ in runs:
        tp, fp, fn, tn, p, r, f = stats(gold, preds[name])
        print("%-22s %4d %4d %4d %4d %10.2f %8.2f %6.2f" % ("jev run " + name, tp, fp, fn, tn, p, r, f))
    kw = {i: any(k in text[i] for k in KEYWORDS) for i in gold}
    tp, fp, fn, tn, p, r, f = stats(gold, kw)
    print("%-22s %4d %4d %4d %4d %10.2f %8.2f %6.2f" % ("keyword scan", tp, fp, fn, tn, p, r, f))

    if len(runs) > 1:
        ps = [stats(gold, preds[n])[4] for n, _, _ in runs]
        rs = [stats(gold, preds[n])[5] for n, _, _ in runs]
        fs = [stats(gold, preds[n])[6] for n, _, _ in runs]
        print("\nacross %d runs: precision %s | recall %s | F1 %s"
              % (len(runs), interval(ps), interval(rs), interval(fs)))
        # Which events the run-to-run spread actually moves. These are the ones a reader
        # should treat as "worth a look" rather than as decided either way.
        unstable = [i for i in sorted(gold)
                    if len({preds[n][i] for n, _, _ in runs}) > 1]
        if unstable:
            print("\nflipped between runs (%d) — read these as provisional, not as decided:" % len(unstable))
            for i in unstable:
                got = " ".join("%s=%s" % (n, preds[n][i]) for n, _, _ in runs)
                print("  %s  %-30s  %s" % (i, got, text[i][:60]))
        else:
            print("\nno event flipped between runs at this cut")

    print("\nscore separation (per run, labeled positives vs routine):")
    overlap = []
    for name, rows, _ in runs:
        pos = sorted(r["attention"] for r in rows if gold.get(r["id"]))
        neg = sorted(r["attention"] for r in rows if r["id"] in gold and not gold[r["id"]])
        if not pos or not neg:
            continue
        print("  %-22s positives %.2f..%.2f | routine %.2f..%.2f"
              % (name, pos[0], pos[-1], neg[0], neg[-1]))
        if neg[-1] >= args.threshold and pos[0] <= args.threshold:
            overlap.append(name)
    if overlap:
        print("  the routine tail reaches the cut in: %s" % ", ".join(overlap))

    print("\n%-9s %-14s %-14s" % ("cut", "precision", "recall"))
    for t in THRESHOLDS:
        ps, rs = [], []
        for name, rows, _ in runs:
            pred = {r["id"]: r["attention"] >= t for r in rows}
            tp, fp, fn, _, p, r, _f = stats(gold, pred)
            ps.append(p if tp + fp else float("nan"))
            rs.append(r if tp + fn else 0.0)
        print("%-9.2f %-14s %-14s" % (t, interval([p for p in ps if p == p] or [float("nan")]),
                                      interval(rs)))

    print("\n--- disagreements at %.2f (jev vs hand labels) ---" % args.threshold)
    shown = False
    for name, rows, _ in runs:
        misses = [r for r in sorted(rows, key=lambda r: -r["attention"])
                  if preds[name][r["id"]] != gold.get(r["id"])]
        for r in misses:
            kind = "false positive" if preds[name][r["id"]] else "missed"
            print("  %-14s %s  %s  att=%.2f  %s"
                  % (kind, name, r["id"], r["attention"], text[r["id"]][:64]))
            if labels[r["id"]].get("note"):
                print("                 label note: %s" % labels[r["id"]]["note"])
            shown = True
    if not shown:
        print("  none")

    print("\n--- where the keyword scan goes wrong ---")
    for i in sorted(gold, key=lambda i: (kw[i] != gold[i], i)):
        if kw[i] != gold[i]:
            print("  %-14s %s  %s" % ("false positive" if kw[i] else "missed", i, text[i][:78]))

    errors = sum(len(e) for _, _, e in runs)
    if errors:
        print("\n%d events came back as service errors across all runs — not judged, not a negative answer"
              % errors)

    if args.min_precision is not None:
        worst = min(stats(gold, preds[n])[4] for n, _, _ in runs)
        if worst < args.min_precision:
            print("\nprecision %.2f fell short of %.2f" % (worst, args.min_precision))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
