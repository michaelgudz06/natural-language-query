"""
Architecture bakeoff (Jacob's ask: test different architectures, not just models).

Compares whole *approaches* on the same test set — accuracy, time, cost — so you
can pick one with data:

  1. single          — one model does everything (route + extraction) in one call
  2. router          — cheap model first; escalate to a strong model only when it's
                       unsure (the confidence "safety net")
  3. orchestrator/executor — a smart model makes the small routing decision, a cheap
                       model does the bulky filter extraction (two roles, two models)

Each architecture is a small function `run(query) -> (plan, calls, latency_ms)`.
`calls` lists every model call with tokens, so cost is real. Deliberately simple —
add an architecture by writing one function and listing it in ARCHITECTURES.

Usage:
  python3 src/arch_bakeoff.py --n 40         # first N cases
  python3 src/arch_bakeoff.py --md           # write docs/ARCH_BAKEOFF.md
Needs OPENROUTER_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openai import OpenAI  # noqa: E402
from planner import openrouter_planner  # noqa: E402
from model_compare import _expected_intents, _model_intents, _score_intents  # noqa: E402

_TEST = os.path.join(os.path.dirname(__file__), "..", "tests", "test_set.json")
_ROUTES = ["POST /deals", "POST /companies", "POST /investors", "POST /people"]

# USD per 1M tokens (in, out) — OpenRouter live 2026-07-22.
PRICES = {
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "google/gemini-3.5-flash-lite": (0.30, 2.50),
    "google/gemini-3.1-flash-lite": (0.25, 1.50),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-5.1": (1.25, 10.00),
    "google/gemma-4-31b-it": (0.12, 0.35),
    "google/gemma-3-27b-it": (0.10, 0.30),
}


def _cost(calls: list[dict]) -> float:
    total = 0.0
    for c in calls:
        pin, pout = PRICES.get(c["model"], (0.0, 0.0))
        total += (c["in"] * pin + c["out"] * pout) / 1e6
    return total


def _call_meta(planner, model, ms) -> dict:
    u = getattr(planner, "last_usage", None)
    return {"model": model, "in": getattr(u, "prompt_tokens", 0) or 0,
            "out": getattr(u, "completion_tokens", 0) or 0, "ms": ms}


# --- the orchestrator: a tiny route-only classifier (small output = fast/cheap) ---
def _route_only(model: str, query: str):
    schema = {"type": "object", "additionalProperties": False,
              "required": ["route", "confidence"],
              "properties": {"route": {"type": "string", "enum": _ROUTES},
                             "confidence": {"type": "number"}}}
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
    t = time.perf_counter()
    r = client.chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "system", "content":
                   "Pick the Fundable API route. deals = funding rounds/raises; "
                   "companies = startups/orgs; investors = VC firms/funds; "
                   "people = founders/execs/angels/partners. Return route + confidence 0-1."},
                  {"role": "user", "content": query}],
        response_format={"type": "json_schema", "json_schema": {"name": "route", "schema": schema, "strict": True}})
    ms = (time.perf_counter() - t) * 1000
    d = json.loads(r.choices[0].message.content)
    u = r.usage
    return d["route"], {"model": model, "in": u.prompt_tokens, "out": u.completion_tokens, "ms": ms}


# --- architectures (each returns run(query, today) -> (plan, calls, latency_ms)) ---
def single(model: str):
    p = openrouter_planner(model)

    def run(q, today):
        t = time.perf_counter()
        plan = p.plan(q, today)
        ms = (time.perf_counter() - t) * 1000
        return plan, [_call_meta(p, model, ms)], ms
    return run


def router(cheap: str, strong: str, threshold: float = 0.75):
    pc, ps = openrouter_planner(cheap), openrouter_planner(strong)

    def run(q, today):
        t = time.perf_counter()
        plan = pc.plan(q, today)
        ms1 = (time.perf_counter() - t) * 1000
        calls = [_call_meta(pc, cheap, ms1)]
        latency = ms1
        if plan.confidence < threshold:            # escalate (sequential)
            t = time.perf_counter()
            plan = ps.plan(q, today)
            ms2 = (time.perf_counter() - t) * 1000
            calls.append(_call_meta(ps, strong, ms2))
            latency = ms1 + ms2
        return plan, calls, latency
    return run


def reflect_retry(model: str, threshold: float = 1.0):
    """(c) Jacob/Michael's idea: if confidence isn't at the bar, re-ask the SAME
    model with its own first answer + reasoning fed back, and take the retry.
    Calibration says errors live in the 90-99% band, so threshold=1.0 targets them."""
    p = openrouter_planner(model)

    def run(q, today):
        t = time.perf_counter()
        plan = p.plan(q, today)
        ms = (time.perf_counter() - t) * 1000
        calls = [_call_meta(p, model, ms)]
        latency = ms
        if plan.confidence < threshold:
            reflect_q = (q + f"\n\n[On a first pass you chose {plan.route} "
                         f"(reasoning: {plan.reasoning}) but weren't fully certain. "
                         "Reconsider carefully — especially whether the user wants the "
                         "companies/people themselves or their funding rounds — and give "
                         "your best final answer.]")
            t = time.perf_counter()
            plan = p.plan(reflect_q, today)
            ms = (time.perf_counter() - t) * 1000
            calls.append(_call_meta(p, model, ms))
            latency += ms
        return plan, calls, latency
    return run


def orchestrator_executor(orch: str, executor: str):
    pe = openrouter_planner(executor)

    def run(q, today):
        # the two calls are independent (route vs. filters), so real latency is the
        # slower of the two, not the sum. We run them back-to-back but report max.
        route, c1 = _route_only(orch, q)
        t = time.perf_counter()
        plan = pe.plan(q, today)
        ms = (time.perf_counter() - t) * 1000
        c2 = _call_meta(pe, executor, ms)
        plan.route = route                          # orchestrator owns the route
        return plan, [c1, c2], max(c1["ms"], ms)
    return run


# The approaches to compare — three architecture families across the models Jacob
# flagged. Add one by writing a function above and a row here.
ARCHITECTURES = {
    # --- (c) reflect-and-retry vs its plain single-model baseline ---
    "single: gemini-3.5-flash-lite (baseline)": single("google/gemini-3.5-flash-lite"),
    "reflect-retry: gemini-3.5-flash-lite (<100%)": reflect_retry("google/gemini-3.5-flash-lite", 1.0),
    # --- single model (one call, route + extraction) ---
    "single: gemini-2.5-flash-lite": single("google/gemini-2.5-flash-lite"),
    "single: gemini-3.5-flash-lite": single("google/gemini-3.5-flash-lite"),
    "single: gpt-4o-mini": single("openai/gpt-4o-mini"),
    "single: gemma-4-31b": single("google/gemma-4-31b-it"),
    # --- confidence router (cheap first, escalate the unsure ones) ---
    "router: flash-lite → 3.5-flash-lite (all-cheap)":
        router("google/gemini-2.5-flash-lite", "google/gemini-3.5-flash-lite"),
    "router: 4o-mini → gpt-5.1 (cheap→expensive)":
        router("openai/gpt-4o-mini", "openai/gpt-5.1"),
    # --- orchestrator/executor (one model routes, another extracts) ---
    "orch/exec: 3.5-flash-lite route + flash-lite extract":
        orchestrator_executor("google/gemini-3.5-flash-lite", "google/gemini-2.5-flash-lite"),
    "orch/exec: gpt-5.1 route + flash-lite extract":
        orchestrator_executor("openai/gpt-5.1", "google/gemini-2.5-flash-lite"),
}


def evaluate(name, run, cases) -> dict:
    routes_ok = 0
    precs, recs, lats, costs, ncalls = [], [], [], [], []
    errors = 0
    for c in cases:
        try:
            plan, calls, latency = run(c["input"], "2026-07-22")
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        routes_ok += int(plan.route == c["expected_route"])
        p, r = _score_intents(_expected_intents(c), _model_intents(plan, c["input"]))
        precs.append(p); recs.append(r); lats.append(latency)
        costs.append(_cost(calls)); ncalls.append(len(calls))
    n = len(cases) - errors
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    ls = sorted(lats)
    return {"name": name, "n": n,
            "route_acc": routes_ok / n if n else 0.0,
            "intent_rec": avg(recs), "intent_prec": avg(precs),
            "cost_per_1k": avg(costs) * 1000,
            "lat_p50": ls[len(ls)//2] if ls else 0,
            "lat_p95": ls[min(len(ls)-1, int(len(ls)*0.95))] if ls else 0,
            "avg_calls": avg(ncalls)}


def to_md(rows) -> str:
    out = ["# Architecture Bakeoff", "",
           "_Same test set, different approaches. Accuracy + time + cost so you can "
           "pick one. OpenRouter, live 2026-07-22._", "",
           "| architecture | route acc | intent R | $/1k | latency p50/p95 | calls/query |",
           "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (-x["route_acc"], x["cost_per_1k"])):
        out.append(f"| {r['name']} | {r['route_acc']:.0%} | {r['intent_rec']:.0%} | "
                   f"${r['cost_per_1k']:.3f} | {r['lat_p50']:.0f}/{r['lat_p95']:.0f}ms | "
                   f"{r['avg_calls']:.2f} |")
    out += ["", "How to read it: pick the cheapest/fastest row that clears your accuracy "
            "bar. If a single cheap model already passes, you don't need the router or the "
            "orchestrator split at all — simpler wins."]
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=0, help="limit to first N cases (0=all)")
    ap.add_argument("--set", type=str, default=_TEST, help="path to a test/eval set json")
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--only", type=str, default="", help="substring filter on architecture names")
    args = ap.parse_args()
    cases = json.load(open(args.set))["cases"]
    if args.n:
        cases = cases[: args.n]
    archs = {k: v for k, v in ARCHITECTURES.items() if args.only.lower() in k.lower()}
    rows = []
    for name, run in archs.items():
        print(f"evaluating: {name} …", file=sys.stderr)
        rows.append(evaluate(name, run, cases))
    md = to_md(rows)
    print(md)
    if args.md:
        p = os.path.join(os.path.dirname(__file__), "..", "docs", "ARCH_BAKEOFF.md")
        open(p, "w").write(md + "\n")
        print(f"\nwrote {p}", file=sys.stderr)
