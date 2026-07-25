"""
Shared types for the intermediate plan (Sprint 2) and the compiled request.

The intermediate plan is what the LLM returns in Phase 1. It deliberately does
NOT contain resolved permalinks or UUIDs — only human display names and the
lookups needed to resolve them. Deterministic code (Phase 2) turns this into a
validated API body.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Route = Literal["POST /deals", "POST /companies", "POST /investors", "POST /people"]
ResolverType = Literal["location", "industry", "company", "investor", "people"]


@dataclass
class ResolverTask:
    type: ResolverType
    query: str                    # human display name, e.g. "San Francisco"
    target_path: str              # dotted path, e.g. "company.locations"
    hint: str | None = None       # optional: CITY / INDUSTRY / SUPER_CATEGORY

    def to_dict(self) -> dict[str, Any]:
        d = {"type": self.type, "query": self.query, "target_path": self.target_path}
        if self.hint:
            d["hint"] = self.hint
        return d


@dataclass
class IntermediatePlan:
    route: Route
    # extracted_filters holds only deterministic-or-nameable values:
    #   round phrases, sizes, dates, employee counts, ipo status, sort phrase,
    #   plus display names that will become resolver_tasks.
    extracted_filters: dict[str, Any] = field(default_factory=dict)
    resolver_tasks: list[ResolverTask] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reasoning: str = ""     # the model's plain-English explanation of its choices
    # locations/industries the user wants EXCLUDED. The API can't exclude, so these
    # are resolved to permalinks and applied client-side (count by subtraction +
    # page post-filter). They never enter the positive filter body.
    excluded_tasks: list[ResolverTask] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "extracted_filters": self.extracted_filters,
            "resolver_tasks": [t.to_dict() for t in self.resolver_tasks],
            "warnings": self.warnings,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IntermediatePlan":
        return cls(
            route=d["route"],
            extracted_filters=d.get("extracted_filters", {}),
            resolver_tasks=[
                ResolverTask(
                    type=t["type"],
                    query=t["query"],
                    target_path=t["target_path"],
                    hint=t.get("hint"),
                )
                for t in d.get("resolver_tasks", [])
            ],
            warnings=d.get("warnings", []),
            confidence=float(d.get("confidence", 0.0)),
        )


# Flat, fully-specified schema handed to the model's structured-output mode.
# Every property is required and typed; no free-form objects — this is what makes
# strict structured output work uniformly across OpenAI, Anthropic, and Gemini
# (a free-form object is rejected by OpenAI/Anthropic strict mode). The model only
# emits DISPLAY NAMES and simple hints; deterministic code turns this into the
# IntermediatePlan (canonical paths, resolver tasks, round mapping).
PLANNER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["route", "reasoning", "locations", "industries", "excluded_locations",
                 "excluded_industries", "investors", "companies", "people",
                 "person_roles", "job_titles", "schools", "past_employers",
                 "round_phrases", "search_query", "sort_hint", "confidence", "warnings"],
    "properties": {
        "route": {"type": "string", "enum": ["POST /deals", "POST /companies", "POST /investors", "POST /people"]},
        "reasoning": {"type": "string"},
        "locations": {"type": "array", "items": {"type": "string"}},
        "industries": {"type": "array", "items": {"type": "string"}},
        "excluded_locations": {"type": "array", "items": {"type": "string"}},
        "excluded_industries": {"type": "array", "items": {"type": "string"}},
        "investors": {"type": "array", "items": {"type": "string"}},
        "companies": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {"type": "string"}},
        # --- person-side filters (POST /people) ---
        "person_roles": {"type": "array", "items": {"type": "string"}},
        "job_titles": {"type": "array", "items": {"type": "string"}},
        "schools": {"type": "array", "items": {"type": "string"}},
        "past_employers": {"type": "array", "items": {"type": "string"}},
        "round_phrases": {"type": "array", "items": {"type": "string"}},
        "search_query": {"type": ["string", "null"]},
        "sort_hint": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}

# Legacy nested schema (kept for reference/tests; no longer sent to models).
INTERMEDIATE_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["route", "extracted_filters", "resolver_tasks", "warnings", "confidence"],
    "properties": {
        "route": {
            "type": "string",
            "enum": ["POST /deals", "POST /companies", "POST /investors", "POST /people"],
        },
        "extracted_filters": {"type": "object"},
        "resolver_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "query", "target_path"],
                "properties": {
                    "type": {"type": "string", "enum": ["location", "industry", "company", "investor", "people"]},
                    "query": {"type": "string"},
                    "target_path": {"type": "string"},
                    "hint": {"type": "string"},
                },
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}
