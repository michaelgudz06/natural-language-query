"""
(a) Confidence calibration — is the model's self-reported confidence trustworthy,
and where should the threshold X be?

Runs the production model over the test set, buckets every case by the confidence
the model reported, and measures the ACTUAL route accuracy in each bucket. If the
buckets line up with reality (90% confident ≈ 90% right), confidence is usable and
X is wherever accuracy falls below your bar. If they don't line up, the number is
miscalibrated and a fixed threshold would be guessing.

Usage:  python3 src/calibrate.py         (needs OPENROUTER_API_KEY)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from planner import load_config, openrouter_planner  # noqa: E402

_TEST = os.path.join(os.path.dirname(__file__), "..", "tests", "test_set.json")


def run() -> dict:
    model = (load_config().get("production") or {}).get("model", "google/gemini-3.5-flash-lite")
    planner = openrouter_planner(model)
    cases = json.load(open(_TEST))["cases"]
    rows = []
    for c in cases:
        try:
            plan = planner.plan(c["input"], "2026-07-23")
        except Exception:  # noqa: BLE001
            continue
        rows.append({"conf": plan.confidence, "ok": plan.route == c["expected_route"]})

    # bucket by confidence (0.5–0.6, …, 0.9–1.0, plus exactly 1.0)
    buckets: dict[str, list] = {}
    for r in rows:
        c = r["conf"]
        if c >= 1.0:
            key = "100%"
        else:
            lo = int(c * 10) * 10
            key = f"{lo}-{lo+10}%"
        buckets.setdefault(key, []).append(r["ok"])

    table = []
    for key in sorted(buckets, key=lambda k: (k == "100%", k)):
        oks = buckets[key]
        table.append({"bucket": key, "n": len(oks), "accuracy": sum(oks) / len(oks)})

    # for each candidate threshold, what would we flag and how good is the rest?
    thresholds = {}
    for X in (0.7, 0.8, 0.9, 0.95, 1.0):
        flagged = [r for r in rows if r["conf"] < X]
        passed = [r for r in rows if r["conf"] >= X]
        thresholds[f"{X:.2f}"] = {
            "flagged_pct": len(flagged) / len(rows),
            "passed_accuracy": (sum(r["ok"] for r in passed) / len(passed)) if passed else None,
            "flagged_accuracy": (sum(r["ok"] for r in flagged) / len(flagged)) if flagged else None,
        }

    return {"model": model, "n": len(rows), "buckets": table, "thresholds": thresholds}


if __name__ == "__main__":
    rep = run()
    print(f"\n  CONFIDENCE CALIBRATION — {rep['model']}  (n={rep['n']})\n")
    print(f"  {'confidence':<14}{'cases':>7}{'actual route acc':>20}")
    print("  " + "-" * 41)
    for b in rep["buckets"]:
        print(f"  {b['bucket']:<14}{b['n']:>7}{b['accuracy']:>19.0%}")
    print("\n  If a threshold X flagged low-confidence queries:")
    print(f"  {'X':<8}{'% flagged':>12}{'accuracy if flagged':>22}{'accuracy if passed':>22}")
    print("  " + "-" * 62)
    for X, t in rep["thresholds"].items():
        fa = "—" if t["flagged_accuracy"] is None else f"{t['flagged_accuracy']:.0%}"
        pa = "—" if t["passed_accuracy"] is None else f"{t['passed_accuracy']:.0%}"
        print(f"  {X:<8}{t['flagged_pct']:>11.0%}{fa:>22}{pa:>22}")
    print("\n  Read: if 'accuracy if flagged' is much LOWER than 'if passed', the")
    print("  confidence number is meaningful and that X is a good threshold.\n")
