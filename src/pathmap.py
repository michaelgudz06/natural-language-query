"""
Canonical target-path mapping for resolver tasks.

The LLM proposes *which* lookups are needed and roughly where they go, but it
doesn't know the exact schema paths (e.g. it emits "locations" instead of
"company.locations"). This module rewrites each resolver task's target_path to
the canonical schema path for its (route, type), so the compiler always writes
resolved values to a real field. Deterministic; keeps the model out of schema
minutiae.

Ambiguities we resolve with a documented default:
  * location on /investors defaults to the PORTFOLIO location
    (company_investments.locations). Investor-HQ ("funds based in SF") is the
    minority case; if the model's original path clearly said investor HQ we keep
    it. See docs/GAPS.md.
"""
from __future__ import annotations

from plan_types import ResolverTask

# (route_short, resolver_type) -> canonical dotted path
_MAP = {
    ("deals", "location"): "company.locations",
    ("deals", "industry"): "company.industries",
    ("deals", "investor"): "investors.investor_ids",
    ("deals", "company"): "company.company_ids",
    ("deals", "people"): "investors.people_ids",

    ("companies", "location"): "company.locations",
    ("companies", "industry"): "company.industries",
    ("companies", "investor"): "investors.investor_ids",
    ("companies", "company"): "identifiers.ids",
    ("companies", "people"): "investors.people_ids",

    ("investors", "location"): "company_investments.locations",
    ("investors", "industry"): "company_investments.industries",
    ("investors", "investor"): "identifiers.ids",
    ("investors", "company"): "company_investments.company_ids",
    ("investors", "people"): "company_investments.company_ids",

    ("people", "location"): "company.locations",
    ("people", "industry"): "company.industries",
    ("people", "investor"): "investor.ids",
    ("people", "company"): "company.ids",
    ("people", "people"): "person.investor_people_ids",
}

# Model-supplied paths we trust as-is (valid schema leaves for that route).
_TRUSTED = {
    "investors": {"investor.locations"},        # explicit investor-HQ location
    "people": {"investor.deals.industries", "investor.deals.super_categories",
               "investor.deals.portfolio_locations", "company.latest_deal.investor_ids"},
}


def canonical_path(route_short: str, task: ResolverTask) -> str:
    orig = (task.target_path or "").strip()
    if orig in _TRUSTED.get(route_short, set()):
        return orig
    return _MAP.get((route_short, task.type), orig or "company.locations")


def normalize_tasks(route_short: str, tasks: list[ResolverTask]) -> list[ResolverTask]:
    for t in tasks:
        t.target_path = canonical_path(route_short, t)
    return tasks
