"""
Sprint 3 + Sprint 5 two-stage routing + Sprint 6 observability: the pipeline.

    sentence
      -> Phase 1: planner.plan()            (LLM or offline heuristic)
      -> enrich with deterministic scalars  (extractors)
      -> canonicalize resolver paths        (pathmap)
      -> Phase 2: resolvers.resolve_all()   (parallel lookups)
      -> compiler.compile()                 (assemble + validate)
      -> PlanResult

TWO-STAGE (spec §5/§14): when a `strong_planner` is supplied, the pipeline runs
the cheap planner first and escalates to the strong one ONLY when the cheap
result is low-confidence or fails schema validation. This is the cost/quality
lever — most queries never touch the expensive model.

Emits a structured per-stage log (route, tier, escalation reason, latency by
stage, validity, clarification) for observability.
"""
from __future__ import annotations

import time
from typing import Any

from compiler import CompileResult, Compiler
from extractors import expand_open_ended_rounds, extract_scalars
from pathmap import normalize_tasks

_FIN_PATH = {"deals": "deal.financing_types", "companies": "latest_deal.financing_types",
             "investors": "company_investments.financing_types",
             "people": "company.latest_deal.financing_types"}
from planner import Planner, get_planner
from resolvers import BaseResolver, get_resolver
from schema import SchemaMap

_ROUTE_SHORT = {
    "POST /deals": "deals", "POST /companies": "companies",
    "POST /investors": "investors", "POST /people": "people",
}


def _model_name(planner) -> str:
    """Best-effort model id for logging/UI (LLMPlanner carries its config)."""
    try:
        return planner.config["models"]["primary"]["name"]
    except Exception:  # noqa: BLE001
        return type(planner).__name__


class Pipeline:
    def __init__(
        self,
        planner: Planner | None = None,
        resolver: BaseResolver | None = None,
        compiler: Compiler | None = None,
        strong_planner: Planner | None = None,
        escalate_confidence_below: float = 0.75,
        price_in_per_m: float = 0.0,
        price_out_per_m: float = 0.0,
    ):
        self.planner = planner or get_planner()
        self.resolver = resolver or get_resolver()
        self.compiler = compiler or Compiler(SchemaMap.load())
        self.strong_planner = strong_planner          # None => single-stage
        self.threshold = escalate_confidence_below
        self.price_in_per_m = price_in_per_m           # USD per 1M tokens
        self.price_out_per_m = price_out_per_m

    # -- one planning+resolve+compile pass ---------------------------------
    def _stage(self, planner: Planner, query: str, today: str):
        t: dict[str, float] = {}
        t0 = time.perf_counter()
        plan = planner.plan(query, today)
        t["plan_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        short = _ROUTE_SHORT[plan.route]
        for path, value in extract_scalars(query, short, today).items():
            plan.extracted_filters.setdefault(path, value)
        # open-ended rounds ('Series C+', 'seed or higher') override the model's
        # single-round output, since '+' means that round AND every higher one.
        expanded = expand_open_ended_rounds(query)
        if expanded:
            plan.extracted_filters[_FIN_PATH[short]] = expanded
        normalize_tasks(short, plan.resolver_tasks)

        t1 = time.perf_counter()
        outcomes = self.resolver.resolve_all(plan.resolver_tasks)
        t["resolve_ms"] = round((time.perf_counter() - t1) * 1000, 1)

        t2 = time.perf_counter()
        result = self.compiler.compile(plan, outcomes, today)
        t["compile_ms"] = round((time.perf_counter() - t2) * 1000, 1)
        t["_resolver_count"] = len(outcomes)
        u = getattr(planner, "last_usage", None)
        t["in_tokens"] = getattr(u, "prompt_tokens", 0) or 0
        t["out_tokens"] = getattr(u, "completion_tokens", 0) or 0
        return result, t, plan

    @staticmethod
    def _plan_snapshot(plan) -> dict:
        return {
            "route": plan.route,
            "confidence": plan.confidence,
            "reasoning": plan.reasoning,
            "looked_up": [{"type": rt.type, "name": rt.query} for rt in plan.resolver_tasks],
        }

    def run(self, query: str, today: str = "2026-07-22") -> dict[str, Any]:
        log: dict[str, Any] = {"query": query, "stages": {}}
        t_start = time.perf_counter()

        result, t_cheap, plan_cheap = self._stage(self.planner, query, today)
        tier = "single" if self.strong_planner is None else "cheap"
        escalated = False
        reason = None
        final_plan = plan_cheap
        trace: dict[str, Any] = {"cheap": self._plan_snapshot(plan_cheap), "strong": None}

        if self.strong_planner is not None and (result.confidence < self.threshold or not result.valid):
            reason = "low_confidence" if result.confidence < self.threshold else "schema_invalid"
            strong_result, t_strong, plan_strong = self._stage(self.strong_planner, query, today)
            trace["strong"] = self._plan_snapshot(plan_strong)
            # Keep the strong result unless it somehow came back worse (invalid).
            if strong_result.valid or not result.valid:
                result, tier, escalated, final_plan = strong_result, "strong", True, plan_strong
                log["stages"]["cheap"] = t_cheap
                log["stages"].update(t_strong)
            else:
                log["stages"].update(t_cheap)
        else:
            log["stages"].update(t_cheap)

        trace["final"] = self._plan_snapshot(final_plan)

        # resolve any EXCLUDED locations/industries (applied client-side, never in
        # the positive body). Surface them so the app can post-filter / subtract.
        exclusions = {"locations": [], "industries": [], "unresolved": []}
        if final_plan.excluded_tasks:
            for oc in self.resolver.resolve_all(final_plan.excluded_tasks):
                if oc.status == "resolved" and oc.chosen:
                    grp = "locations" if oc.task.type == "location" else "industries"
                    exclusions[grp].append({"permalink": oc.chosen.value, "label": oc.chosen.label})
                else:
                    exclusions["unresolved"].append(oc.task.query)

        log["stages"]["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
        # tokens + cost (summed across stages; escalation adds the strong call)
        stages = log["stages"]
        tin = stages.get("in_tokens", 0) + (stages.get("cheap", {}).get("in_tokens", 0) if escalated else 0)
        tout = stages.get("out_tokens", 0) + (stages.get("cheap", {}).get("out_tokens", 0) if escalated else 0)
        cost = (tin * self.price_in_per_m + tout * self.price_out_per_m) / 1e6

        log["route"] = result.route
        log["planner"] = type(self.planner).__name__
        log["model"] = _model_name(self.planner)
        log["tokens_in"] = tin
        log["tokens_out"] = tout
        log["cost_usd"] = cost
        log["cost_per_1k"] = cost * 1000
        log["think_ms"] = stages.get("plan_ms", 0)
        log["tier"] = tier
        log["escalated"] = escalated
        log["escalate_reason"] = reason
        log["resolver_calls"] = log["stages"].pop("_resolver_count", None)
        log["valid"] = result.valid
        log["errors"] = result.errors
        log["needs_clarification"] = bool(result.needs_clarification)

        out = result.to_dict()
        out["_log"] = log
        out["trace"] = trace
        out["exclusions"] = exclusions
        return out


def plan_query(query: str, today: str = "2026-07-22") -> dict[str, Any]:
    """Convenience one-shot entrypoint (single-stage, auto-selected planner)."""
    return Pipeline().run(query, today)


def production_pipeline() -> Pipeline:
    """The production setup: ONE model, no router (chosen by the architecture
    bakeoff — see docs/ARCH_BAKEOFF.md). Simple and fast. Model id comes from
    config/planner.yaml > production.model. Requires OPENROUTER_API_KEY."""
    from planner import load_config, openrouter_planner

    cfg = load_config()
    prod = cfg.get("production") or {}
    model = prod.get("model", "google/gemini-3.5-flash-lite")
    return Pipeline(planner=openrouter_planner(model), resolver=get_resolver(), compiler=Compiler(),
                    price_in_per_m=prod.get("price_in_per_m", 0.30),
                    price_out_per_m=prod.get("price_out_per_m", 2.50))


def two_stage_pipeline(cheap_model: str | None = None, strong_model: str | None = None) -> Pipeline:
    """Build the cheap->strong escalating pipeline from config/planner.yaml.
    Requires OPENROUTER_API_KEY. Resolver/compiler default to live/schema."""
    from planner import load_config, openrouter_planner

    cfg = load_config()
    cheap = openrouter_planner(cheap_model or cfg["models"]["cheap"]["name"])
    strong = openrouter_planner(strong_model or cfg["models"]["primary"]["name"])
    thr = cfg["models"]["fallback_to_primary_when"]["confidence_below"]
    return Pipeline(planner=cheap, resolver=get_resolver(), compiler=Compiler(),
                    strong_planner=strong, escalate_confidence_below=thr)
