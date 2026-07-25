"""
Model comparison harness (Jacob's ask: "see how it performs with different models").

For each model it runs ONLY the planning phase over the test set and scores what
the MODEL is responsible for — route choice and which entities/filters it pulled
out — isolated from the deterministic resolver/API layer (which is model-agnostic).
That's what a model bakeoff should measure.

Metrics per model:
  route accuracy | intent recall/precision (locations+industries+rounds the model
  identified vs. expected) | JSON-valid rate | avg in/out tokens | cost per 1k
  | model latency p50/p95

Usage:
  python3 src/model_compare.py                 # full test set, default lineup
  python3 src/model_compare.py --n 30          # first N cases (faster)
  python3 src/model_compare.py --md            # write docs/MODEL_COMPARISON.md
Needs OPENROUTER_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from planner import openrouter_planner  # noqa: E402
from extractors import expand_open_ended_rounds  # noqa: E402

_TEST = os.path.join(os.path.dirname(__file__), "..", "tests", "test_set.json")

# lineup — cheap/fast candidates for the router's first tier + a strong reference.
# pricing = USD per 1M tokens (in, out), from OpenRouter live 2026-07-22.
LINEUP = [
    ("google/gemini-2.5-flash-lite", 0.10, 0.40),
    ("google/gemini-3.5-flash-lite", 0.30, 2.50),   # Jacob asked about this one
    ("google/gemini-3.1-flash-lite", 0.25, 1.50),
    ("openai/gpt-4o-mini", 0.15, 0.60),             # current cheap tier
    ("google/gemini-2.5-flash", 0.30, 2.50),
    ("openai/gpt-5.1", 1.25, 10.00),                # strong reference / ceiling
    ("google/gemma-4-31b-it", 0.12, 0.35),          # Jacob likes this one
    ("google/gemma-3-27b-it", 0.10, 0.30),
    ("qwen/qwen3.6-flash", 0.188, 1.125),          # Jacob asked
    ("deepseek/deepseek-v4-flash", 0.098, 0.196),  # Jacob asked
    ("minimax/minimax-m2.7", 0.30, 1.20),          # Jacob asked
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _expected_intents(case: dict) -> set[tuple[str, str]]:
    """What the model SHOULD surface: resolver entities + round intent."""
    out = {(r["type"], _norm(r["query"])) for r in case.get("expected_resolvers", [])}
    # rounds: if the expected filter paths include financing_types, expect a round
    if any("financing_types" in p for p in case.get("expected_filter_paths", [])):
        out.add(("round", "*"))
    return out


def _model_intents(plan, query: str) -> set[tuple[str, str]]:
    out = {(t.type, _norm(t.query)) for t in plan.resolver_tasks}
    rounds = plan.extracted_filters.get("deal.financing_types") \
        or plan.extracted_filters.get("latest_deal.financing_types") \
        or plan.extracted_filters.get("company_investments.financing_types") \
        or plan.extracted_filters.get("company.latest_deal.financing_types")
    if rounds or expand_open_ended_rounds(query):
        out.add(("round", "*"))
    return out


def _score_intents(exp: set, got: set) -> tuple[float, float]:
    # collapse round intent to presence; match entities by (type, normalized name)
    def has_round(s):
        return any(t == "round" for t, _ in s)
    exp_e = {x for x in exp if x[0] != "round"}
    got_e = {x for x in got if x[0] != "round"}
    tp = len(exp_e & got_e) + (1 if has_round(exp) and has_round(got) else 0)
    ep = len(exp_e) + (1 if has_round(exp) else 0)
    gp = len(got_e) + (1 if has_round(got) else 0)
    rec = tp / ep if ep else 1.0
    prec = tp / gp if gp else 1.0
    return prec, rec


def run_model(model: str, pin: float, pout: float, cases: list) -> dict:
    planner = openrouter_planner(model)
    routes_ok = valid = 0
    precs, recs, lats, in_toks, out_toks = [], [], [], [], []
    errors = 0
    for c in cases:
        try:
            t0 = time.perf_counter()
            plan = planner.plan(c["input"], "2026-07-22")
            lats.append((time.perf_counter() - t0) * 1000)
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        routes_ok += int(plan.route == c["expected_route"])
        valid += 1  # flat schema is always structurally valid if it parsed
        p, r = _score_intents(_expected_intents(c), _model_intents(plan, c["input"]))
        precs.append(p); recs.append(r)
        u = getattr(planner, "last_usage", None)
        if u:
            in_toks.append(getattr(u, "prompt_tokens", 0) or 0)
            out_toks.append(getattr(u, "completion_tokens", 0) or 0)
    n = len(cases) - errors
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    lat_s = sorted(lats)
    ai, ao = avg(in_toks), avg(out_toks)
    return {
        "model": model, "n": n, "errors": errors,
        "route_acc": routes_ok / n if n else 0.0,
        "intent_prec": avg(precs), "intent_rec": avg(recs),
        "avg_in": ai, "avg_out": ao,
        "cost_per_1k": (ai * pin + ao * pout) / 1e6 * 1000,
        "lat_p50": lat_s[len(lat_s)//2] if lat_s else 0,
        "lat_p95": lat_s[min(len(lat_s)-1, int(len(lat_s)*0.95))] if lat_s else 0,
    }


def to_md(rows: list) -> str:
    out = ["# Model Comparison", "",
           "_Planning-phase quality by model (route + intent extraction), isolated from "
           "the deterministic resolver/API layer. OpenRouter, live 2026-07-22._", "",
           "| model | route acc | intent R | intent P | $/1k | tok in/out | lat p50/p95 |",
           "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: -x["route_acc"]):
        out.append(f"| `{r['model']}` | {r['route_acc']:.0%} | {r['intent_rec']:.0%} | "
                   f"{r['intent_prec']:.0%} | ${r['cost_per_1k']:.3f} | "
                   f"{r['avg_in']:.0f}/{r['avg_out']:.0f} | {r['lat_p50']:.0f}/{r['lat_p95']:.0f}ms |")
    out += ["", "Route acc = % of cases routed to the correct endpoint. Intent R/P = "
            "recall/precision on the locations, industries and round-intent the model "
            "extracted vs. the labeled set. Cost is planning only (one call; the router "
            "escalates ~25% of queries to the strong tier on top of this)."]
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=0, help="limit to first N cases")
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--models", type=str, default="", help="comma-separated model ids override")
    args = ap.parse_args()

    cases = json.load(open(_TEST))["cases"]
    if args.n:
        cases = cases[: args.n]
    lineup = LINEUP
    if args.models:
        ids = args.models.split(",")
        price = {m: (pin, pout) for m, pin, pout in LINEUP}
        lineup = [(m, *price.get(m, (0.0, 0.0))) for m in ids]

    rows = []
    for model, pin, pout in lineup:
        print(f"running {model} over {len(cases)} cases…", file=sys.stderr)
        rows.append(run_model(model, pin, pout, cases))
    md = to_md(rows)
    print(md)
    if args.md:
        p = os.path.join(os.path.dirname(__file__), "..", "docs", "MODEL_COMPARISON.md")
        open(p, "w").write(md + "\n")
        print(f"\nwrote {p}", file=sys.stderr)
