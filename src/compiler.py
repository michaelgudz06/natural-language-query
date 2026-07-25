"""
Sprint 3: deterministic compiler.

Takes the intermediate plan (Phase 1) + resolved values (Phase 2 resolvers) and
assembles the final Fundable request body, then validates it against the schema
map. The LLM never touches this stage — it is pure, testable code.

Responsibilities:
  * Map round/financing phrases -> financing_type objects (config/round_types.yaml).
  * Place resolved permalinks/UUIDs at their target paths.
  * Map deterministic filters: sizes ($), dates ("since 2023"), employee counts,
    ipo status, sort phrases.
  * Enforce industry-first: if a block ends up with both search_query and
    industries, structured wins (drop search_query) per spec §7.
  * Apply compiler hygiene + schema validation before returning.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from plan_types import IntermediatePlan
from resolvers import ResolverOutcome
from schema import SchemaMap

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROUND_TYPES_PATH = os.path.join(_HERE, "..", "config", "round_types.yaml")

_ROUTE_SHORT = {
    "POST /deals": "deals",
    "POST /companies": "companies",
    "POST /investors": "investors",
    "POST /people": "people",
}


@dataclass
class CompileResult:
    route: str
    body: dict[str, Any]
    resolver_calls: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_clarification: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    valid: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "body": self.body,
            "resolver_calls": self.resolver_calls,
            "warnings": self.warnings,
            "needs_clarification": self.needs_clarification,
            "confidence": self.confidence,
            "valid": self.valid,
            "errors": self.errors,
        }


class Compiler:
    def __init__(self, schema: SchemaMap | None = None, planner_config: dict | None = None):
        self.schema = schema or SchemaMap.load()
        with open(_ROUND_TYPES_PATH) as f:
            rt = yaml.safe_load(f)
        self.round_phrases = {k.lower(): v for k, v in rt["phrases"].items()}
        self.round_expansions = {k.lower(): v for k, v in rt["expansions"].items()}

    # -- public -------------------------------------------------------------
    def compile(self, plan: IntermediatePlan, outcomes: list[ResolverOutcome], today: str) -> CompileResult:
        route = plan.route
        short = _ROUTE_SHORT[route]
        body: dict[str, Any] = {}
        warnings = list(plan.warnings)
        needs_clar: list[dict[str, Any]] = []
        resolver_calls: list[dict[str, Any]] = []

        # 1) deterministic extracted filters
        for path, value in plan.extracted_filters.items():
            if path.endswith("financing_types"):
                _set_at(body, path, self._map_financing(value))
            elif path == "sort_by":
                _set_at(body, "sort_by", value)
            else:
                _set_at(body, path, value)

        # 2) resolved lookups -> permalinks/UUIDs at their target paths
        for oc in outcomes:
            resolver_calls.append(_resolver_call_record(oc))
            if oc.status == "resolved" and oc.chosen:
                _append_at(body, oc.task.target_path, oc.chosen.value)
            elif oc.status == "needs_clarification":
                needs_clar.append({
                    "message": f"Did you mean one of these for “{oc.task.query}”?",
                    "target_path": oc.task.target_path,
                    "type": oc.task.type, "query": oc.task.query,
                    "options": [{"label": c.label, "value": c.value, "kind": c.kind} for c in oc.candidates],
                })
            elif oc.status == "not_found":
                warnings.append(f"No match found for “{oc.task.query}” ({oc.task.type}); filter dropped.")

        # 3) industry-first enforcement (structured beats semantic) — spec §7
        self._enforce_industry_first(short, body, warnings)

        # 4) defaults: sort + page_size
        if "sort_by" not in body:
            default_sort = self.schema.default_sort(route)
            # people default sort may be relevance-controlled; still set the schema default
            if default_sort:
                body["sort_by"] = default_sort
        body.setdefault("page_size", 25)

        # 5) hygiene + schema validation
        res = self.schema.validate(route, body)
        cleaned = res.cleaned or body

        # search_query overrides sort_by on some routes -> warn, drop the sort
        self._warn_sort_override(short, cleaned, warnings)

        # safety net: never let a filter-less query look successful. If nothing
        # but pagination/sort survived (e.g. the only filter was an industry that
        # hit a clarification), flag it loudly instead of silently matching all.
        if not _has_real_filter(cleaned):
            warnings.append(
                "No filters resolved — this query would match everything. "
                "Likely an unresolved industry/location above; pick a clarification "
                "option or rephrase."
            )
            confidence_penalty = True
        else:
            confidence_penalty = False

        confidence = plan.confidence
        if needs_clar:
            confidence = min(confidence, 0.5)
        if confidence_penalty:
            confidence = min(confidence, 0.3)

        return CompileResult(
            route=route,
            body=cleaned,
            resolver_calls=resolver_calls,
            warnings=warnings,
            needs_clarification=needs_clar,
            confidence=round(confidence, 2),
            valid=res.ok,
            errors=res.errors,
        )

    # -- mappers ------------------------------------------------------------
    def _map_financing(self, phrases: Any) -> list[dict[str, Any]]:
        if isinstance(phrases, str):
            phrases = [phrases]
        out: list[dict[str, Any]] = []
        for p in phrases:
            if isinstance(p, dict):            # already an object
                out.append(p)
                continue
            key = re.sub(r"\s+", " ", str(p).strip().lower())
            if key in self.round_expansions:
                out.extend(self.round_expansions[key])
            elif key in self.round_phrases:
                out.append(self.round_phrases[key])
            # unknown phrase -> silently skipped (planner shouldn't emit these)
        # de-dupe
        seen, deduped = set(), []
        for o in out:
            sig = (o["type"], o.get("pre", False), o.get("extension", False))
            if sig not in seen:
                seen.add(sig)
                deduped.append(o)
        return deduped

    def _enforce_industry_first(self, short: str, body: dict, warnings: list[str]):
        blocks = {
            "companies": ["company"],
            "investors": ["company_investments"],
            "people": ["company", "investor.deals"],
            "deals": [],   # /deals.company has no search_query anyway
        }[short]
        for bpath in blocks:
            blk = _dig(body, bpath)
            if not isinstance(blk, dict):
                continue
            has_structured = bool(blk.get("industries") or blk.get("super_categories"))
            if has_structured and "search_query" in blk:
                blk.pop("search_query", None)
                blk.pop("min_relevance", None)
                warnings.append(
                    f"{bpath}: dropped search_query in favor of structured industry filter."
                )

    def _warn_sort_override(self, short: str, body: dict, warnings: list[str]):
        if short == "companies":
            if _dig(body, "company.search_query") and "sort_by" in body:
                warnings.append("search_query set: API will order by relevance and ignore sort_by.")
        if short == "people":
            if (_dig(body, "company.search_query") or _dig(body, "investor.deals.search_query")) and "sort_by" in body:
                warnings.append("search_query set: API will order by relevance and ignore sort_by.")


# ---------------------------------------------------------------------------
# dotted-path helpers
# ---------------------------------------------------------------------------
def _set_at(body: dict, dotted: str, value: Any):
    parts = dotted.split(".")
    cur = body
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _append_at(body: dict, dotted: str, value: Any):
    parts = dotted.split(".")
    cur = body
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    arr = cur.setdefault(parts[-1], [])
    if value not in arr:
        arr.append(value)


def _dig(body: dict, dotted: str) -> Any:
    cur = body
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _has_real_filter(body: dict[str, Any]) -> bool:
    """True if the body carries any actual filter (not just pagination/sort)."""
    ignore = {"page", "page_size", "sort_by", "person_type"}
    for k, v in body.items():
        if k in ignore:
            continue
        if isinstance(v, dict) and len(v) > 0:
            return True
        if isinstance(v, list) and len(v) > 0:
            return True
        if v not in (None, "", {}, []):
            return True
    return False


def _resolver_call_record(oc: ResolverOutcome) -> dict[str, Any]:
    endpoint = {
        "location": "GET /location/search",
        "industry": "GET /industry/search",
        "company": "GET /company/search",
        "investor": "GET /investor/search",
        "people": "GET /people/search",
    }[oc.task.type]
    return {
        "endpoint": endpoint,
        "type": oc.task.type,
        "params": {"name": oc.task.query},
        "target_path": oc.task.target_path,
        "status": oc.status,
        "chosen_label": oc.chosen.label if oc.chosen else None,
        "chosen_value": oc.chosen.value if oc.chosen else None,
        "options": [{"label": c.label, "value": c.value} for c in oc.candidates][:5],
    }
