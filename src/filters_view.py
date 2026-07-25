"""
Turn a compiled request body into the list of FILTERS it enables, in plain
language. Every populated field in the body is one active filter — this makes
that explicit for the demo AND is the exact mapping a product would use to flip
on the matching controls in a filter sidebar.

describe_filters(route, body, resolver_calls) -> list of:
    {"filter": "Location", "value": "Boston", "path": "company.locations"}

`path` is the dotted body path (what a UI would bind the control to); `value` is
the human label (resolved names come from resolver_calls, not raw permalinks).
"""
from __future__ import annotations

from typing import Any

# last-path-segment -> human filter label (field names are consistent across routes)
_LABEL = {
    "locations": "Location",
    "industries": "Industry",
    "super_categories": "Category",
    "portfolio_locations": "Portfolio location",
    "investor_locations": "Investor location",
    "financing_types": "Round",
    "size_min": "Deal size ≥", "size_max": "Deal size ≤",
    "deal_size_min": "Deal size ≥", "deal_size_max": "Deal size ≤",
    "date_start": "Date from", "date_end": "Date to",
    "deal_start_date": "Date from", "deal_end_date": "Date to",
    "created_start": "Added from", "created_end": "Added to",
    "total_raised_min": "Total raised ≥", "total_raised_max": "Total raised ≤",
    "employee_count": "Employees", "portfolio_employee_count": "Portfolio employees",
    "ipo_status": "Status",
    "search_query": "Semantic search",
    "investor_ids": "Investor", "people_ids": "Angel / partner",
    "investor_people_ids": "Backed by (person)", "company_ids": "Company",
    "ids": "Identifier", "domains": "Domain",
    "roles": "Role", "job_titles": "Job title", "contact_types": "Has contact",
    "education_schools": "School", "linkedin_companies": "Past employer",
    "investment_type": "Investment type", "only_lead_deals": "Lead deals only",
    "min_matching_deals": "Min matching deals", "person_type": "Person type",
    "min_relevance": "Match threshold", "sort_by": "Sort",
}

_SKIP = {"page", "page_size"}
_SERIES = ["SERIES_" + c for c in "ABCDEFGHIJKLM"]


def _money(n: float) -> str:
    n = float(n)
    if n >= 1e9:
        return f"${n/1e9:.1f}B".replace(".0B", "B")
    if n >= 1e6:
        return f"${n/1e6:.0f}M"
    if n >= 1e3:
        return f"${n/1e3:.0f}K"
    return f"${n:.0f}"


def _round_label(ft: dict) -> str:
    t = ft.get("type", "")
    if t == "SEED":
        base = "Pre-seed" if ft.get("pre") else "Seed"
    elif t.startswith("SERIES_"):
        base = "Series " + t.split("_", 1)[1]
    else:
        base = t.replace("_", " ").title()
    if ft.get("extension"):
        base += " ext"
    return base


def _summarize_rounds(fts: list[dict]) -> str:
    types = [f.get("type") for f in fts]
    series = [t for t in types if t in _SERIES]
    # contiguous run up to Series M -> "Series X+"
    if len(series) > 2 and series == _SERIES[_SERIES.index(series[0]):]:
        start = series[0].split("_", 1)[1]
        prefix = "Seed, " if "SEED" in types else ""
        return f"{prefix}Series {start}+"
    return ", ".join(_round_label(f) for f in fts)


def describe_filters(route: str, body: dict, resolver_calls: list | None = None) -> list[dict[str, Any]]:
    # permalink/uuid -> human label, from the live lookups
    labels: dict[str, str] = {}
    for c in resolver_calls or []:
        if c.get("chosen_value") and c.get("chosen_label"):
            labels[c["chosen_value"]] = c["chosen_label"]

    out: list[dict[str, Any]] = []

    def walk(node: dict, prefix: str):
        for key, val in node.items():
            if key in _SKIP or val in (None, "", [], {}):
                continue
            path = f"{prefix}{key}"
            if isinstance(val, dict):
                walk(val, path + ".")
                continue
            label = _LABEL.get(key, key.replace("_", " ").title())
            out.append({"filter": label, "value": _format(key, val, labels), "path": path})

    walk(body, "")
    # sort_by is a control but not really a "filter" — keep it last
    out.sort(key=lambda x: x["filter"] == "Sort")
    return out


def _format(key: str, val: Any, labels: dict[str, str]) -> str:
    if key == "financing_types" and isinstance(val, list):
        return _summarize_rounds(val)
    if key in ("size_min", "size_max", "deal_size_min", "deal_size_max",
               "total_raised_min", "total_raised_max") and isinstance(val, (int, float)):
        return _money(val)
    if key == "only_lead_deals":
        return "Yes" if val else "No"
    if isinstance(val, list):
        return ", ".join(str(labels.get(v, v)) for v in val)
    if key == "sort_by":
        return str(val).replace("_", " ")
    return str(labels.get(val, val))
