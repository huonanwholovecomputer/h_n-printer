"""Carry the hand labels of the v1 corpus onto the rebuilt corpus, and show the working.

The v1 fixture clustered on text with every digit blanked, so it described events that do
not exist: `成功 1，失败 0` and `成功 3，失败 2` were one event, and whichever line the
builder kept as the representative is the one a person labeled. Its 125 labels therefore
attach to lines, not to conditions, and cannot simply be re-keyed onto a corpus that
separates them properly.

So this maps them the only honest way — by putting both corpora through the current
normalizer and matching — and it refuses to guess where that is not enough:

  inherit   the v1 text normalizes to exactly the new line. The label is carried over; where
            several v1 events collapse into one, a positive anywhere wins, and the merge is
            printed because that pair is what the rebuild was for.
  manual    the v1 text normalizes to the same *shape* but a different measurement, i.e. v1
            had merged this event with a sibling. No v1 label covers it, so it needs a
            judgment: legacy/labels_v2_manual.json must speak for it.
  orphan    nothing in v1 resembles it; also needs a judgment.

A missing judgment is an error, not a default. A corpus that quietly assumes "not a fault"
about a line nobody has read is the same class of bug this whole exercise is fixing.

    python tools/log_triage.py --write-fixture triage_fixture.jsonl   # rebuild the corpus
    python tools/relabel.py                                          # carry the labels
    python tools/verify_redaction.py                                 # the rules still hold
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from log_triage import normalize  # noqa: E402

LEGACY = HERE / "legacy"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def canon(text: str) -> str:
    """Shape of a line with every value erased: numbers, placeholders and spacing gone.

    Two lines with the same shape report the same event about different instances — or about
    different measurements, which is the case this cannot tell apart and therefore will not.
    """
    text = re.sub(r"<[A-Z][A-Z0-9_]*>", "", text)
    return re.sub(r"\s+", " ", re.sub(r"\d+", "", text)).strip()


def main() -> int:
    new = load_jsonl(HERE / "triage_fixture.jsonl")
    old = load_jsonl(LEGACY / "triage_fixture_v1.jsonl")
    old_labels = json.loads((LEGACY / "labels_v1.json").read_text(encoding="utf-8"))
    manual = json.loads((LEGACY / "labels_v2_manual.json").read_text(encoding="utf-8"))["labels"]

    by_text: dict[str, list[str]] = {}
    by_canon: dict[str, list[str]] = {}
    for ev in old:
        text = normalize(ev["sample_raw"])
        by_text.setdefault(text, []).append(ev["id"])
        by_canon.setdefault(canon(text), []).append(ev["id"])

    labels, merges, tier = {}, [], {"inherit": 0, "manual": 0}
    judging_needed = []

    for ev in new:
        line = ev["line"]
        sources = by_text.get(line)
        if sources:
            positives = [s for s in sources if old_labels[s].get("needs_attention")]
            if len(sources) > 1:
                merges.append((ev, sources, positives and len(positives) != len(sources)))
            notes = ["v1 %s: %s" % (s, old_labels[s]["note"]) for s in sources
                     if old_labels[s].get("note")]
            entry = {
                "needs_attention": bool(positives),
                "from": "v1 " + ",".join(sources),
            }
            if any(old_labels[s].get("borderline") for s in sources):
                entry["borderline"] = True
            if notes:
                entry["note"] = " | ".join(notes)
            labels[ev["id"]] = entry
            tier["inherit"] += 1
            continue

        # v1 folded this line into a sibling; only a human can say what it means.
        shape = by_canon.get(canon(line))
        tier_label = "manual" if shape else "orphan"
        hand = manual.get(line)
        if hand is None:
            judging_needed.append((ev, tier_label, shape))
            continue
        labels[ev["id"]] = {**hand, "from": "v2"}
        tier["manual"] += 1

    # Every v1 positive must survive the migration, or the benchmark lost its point.
    carried = {s for ev in new for s in by_text.get(ev["line"], [])}
    lost = [i for i, v in old_labels.items() if v.get("needs_attention") and i not in carried]

    print("v1 events: %d  ->  new events: %d" % (len(old), len(new)))
    print("  inherited a v1 label: %d" % tier["inherit"])
    print("  judged by hand (v2):  %d" % tier["manual"])
    print("  unlabeled:            %d" % len(judging_needed))
    positives = [i for i, v in labels.items() if v["needs_attention"]]
    print("  positives in the new corpus: %d  (%s)"
          % (len(positives), ", ".join(sorted(positives))))

    if merges:
        print("\nv1 events that are now one event (a positive anywhere wins):")
        for ev, sources, conflict in merges:
            kind = "  [v1 disagreed with itself]" if conflict else ""
            print("  %s  <- %s%s" % (ev["id"], ",".join(sources), kind))
            print("      %s" % ev["line"][:96])

    if lost:
        print("\nv1 positives with no counterpart in the new corpus: %s" % ", ".join(sorted(lost)))

    if judging_needed:
        print("\nNEEDS A JUDGMENT — add each line to legacy/labels_v2_manual.json:")
        for ev, kind, shape in judging_needed:
            print("  %s  %s [%dx]  %s" % (ev["id"], kind, ev["count"], ev["line"][:96]))
            if shape:
                print("      v1 shape shared with: %s" % ", ".join(shape))

    (HERE / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\nwrote %d labels to labels.json" % len(labels))

    if judging_needed or lost:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
