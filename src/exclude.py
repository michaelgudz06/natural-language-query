"""
Client-side exclusion (app layer) — because Fundable's API is include-only.

To answer "fintech, Series B+, everywhere but America":
  * COUNT is exact via subtraction:
      net = count(base query) − count(base query AND in the excluded locations)
    Every company is either in the excluded set or not, so the subtraction is exact
    at any granularity (city / state / country / continent).
  * RESULTS: fetch a page of the base query and drop any row whose location matches
    an excluded permalink (results carry the full city/state/country/region chain).

This stays out of the query planner (which remains simple, single-model); it's a
small helper the product calls with its own API-fetch function.
"""
from __future__ import annotations

import copy
from typing import Any, Callable

_ROUTE_SHORT = {"POST /deals": "deals", "POST /companies": "companies",
                "POST /investors": "investors", "POST /people": "people"}
_COMPANY_BLOCK = {"deals": "company", "companies": "company",
                  "investors": "company_investments", "people": "company"}
_LEVELS = ("city", "state", "country", "region")


def apply_location_exclusion(
    route: str,
    body: dict[str, Any],
    excluded_locations: list[dict],   # [{"permalink":..., "label":...}]
    fetch: Callable[[dict, int], dict],   # (body, page_size) -> {"count": int, "companies": [..]}
) -> dict[str, Any] | None:
    """Returns the exclusion result, or None if there's nothing to exclude or the
    route doesn't carry company locations to filter on."""
    perms = [e["permalink"] for e in excluded_locations if e.get("permalink")]
    if not perms:
        return None
    block = _COMPANY_BLOCK[_ROUTE_SHORT[route]]

    # exact net count by subtraction
    total = fetch(body, 1).get("count")
    excl_body = copy.deepcopy(body)
    excl_body.setdefault(block, {})["locations"] = perms      # base AND in-excluded
    in_excluded = fetch(excl_body, 1).get("count")
    net = None
    if isinstance(total, int) and isinstance(in_excluded, int):
        net = max(total - in_excluded, 0)

    # a page of real results, excluded rows removed
    page = fetch(body, 40).get("companies", []) or []
    permset = set(perms)
    kept, removed = [], []
    for c in page:
        loc = c.get("location") or {}
        row_permalinks = {(loc.get(lvl) or {}).get("permalink") for lvl in _LEVELS}
        (removed if row_permalinks & permset else kept).append(c.get("name"))

    return {
        "labels": [e["label"] for e in excluded_locations if e.get("permalink")],
        "total": total, "in_excluded": in_excluded, "net": net,
        "kept_sample": [n for n in kept if n][:6],
        "removed_sample": [n for n in removed if n][:6],
        "page_kept": len(kept), "page_size": len(page),
    }
