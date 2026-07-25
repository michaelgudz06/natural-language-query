"""
Sprint 3: deterministic scalar extractors (money, dates, employee count, ipo,
size). These are things the planner *could* emit, but which are safer to derive
deterministically from the sentence so numbers are never hallucinated.

The pipeline runs these to enrich plan.extracted_filters with the right dotted
paths for the chosen route. Kept separate from the planner so they're unit-testable.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "..", "config", "round_types.yaml")) as _f:
    _RT = yaml.safe_load(_f)
_ROUND_PHRASES = sorted(_RT["phrases"].keys(), key=len, reverse=True)  # longest first
_ROUND_EXPANSIONS = list(_RT["expansions"].keys())

# where financing_types live per route
_FIN_PATH = {
    "deals": "deal.financing_types",
    "companies": "latest_deal.financing_types",
    "investors": "company_investments.financing_types",
    "people": "company.latest_deal.financing_types",
}

# sort-phrase -> sort_by per route (mirrors config/planner.yaml)
_SORT = {
    "recent": {"deals": "most_recent_deal", "companies": "most_recent_raise", "investors": "most_recent_deal", "people": "most_recent_deal_date"},
    "latest": {"deals": "most_recent_deal", "companies": "most_recent_raise", "investors": "most_recent_deal", "people": "most_recent_deal_date"},
    "newest": {"deals": "most_recent_deal", "companies": "most_recent_founded"},
    "oldest": {"deals": "oldest_deal", "companies": "oldest_raise"},
    "largest raise": {"deals": "largest_raise", "companies": "largest_total_raise"},
    "biggest": {"deals": "largest_raise", "companies": "largest_total_raise"},
    "biggest raise": {"deals": "largest_raise", "companies": "largest_total_raise"},
    "smallest raise": {"deals": "smallest_raise", "companies": "smallest_total_raise"},
    "most active": {"investors": "recent_deals"},
    "most deals": {"investors": "total_deals", "people": "total_deal_count"},
    "most funding rounds": {"companies": "most_funding_rounds"},
    "most investors": {"companies": "most_investors"},
    "most lead deals": {"investors": "deals_led_ltm", "people": "lead_deal_count_last_12_months"},
}

# route-short -> where deal-level size/date filters live
_DEAL_BLOCK = {
    "deals": "deal",
    "companies": "latest_deal",
    "investors": "company_investments",   # uses deal_size_*/deal_*_date names
    "people": "company.latest_deal",
}
_COMPANY_BLOCK = {
    "deals": "company",
    "companies": "company",
    "investors": "company_investments",
    "people": "company",
}

_MULT = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000,
         "mm": 1_000_000, "b": 1_000_000_000, "billion": 1_000_000_000}


def _money(token: str) -> float | None:
    m = re.match(r"\$?\s*([\d,.]+)\s*(k|m|mm|b|thousand|million|billion)?", token.strip(), re.I)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    return num * _MULT.get(unit, 1)


def extract_scalars(query: str, short: str, today: str) -> dict:
    """Return {dotted_path: value} extra filters derived deterministically."""
    q = query.lower()
    out: dict = {}
    deal_block = _DEAL_BLOCK[short]
    comp_block = _COMPANY_BLOCK[short]
    # investors block uses deal_size_min/max + deal_start_date/deal_end_date
    size_min_key = "deal_size_min" if short == "investors" else "size_min"
    size_max_key = "deal_size_max" if short == "investors" else "size_max"
    date_start_key = "deal_start_date" if short == "investors" else "date_start"
    date_end_key = "deal_end_date" if short == "investors" else "date_end"

    # Does the amount describe a single round/deal, or a company's total raised?
    # "total" or "raised" (without deal/round words) -> total_raised on the company
    # block; otherwise it's a per-deal size.
    is_total = bool(re.search(r"\btotal\b", q)) or (
        re.search(r"\braised\b", q) and not re.search(r"\b(deal|deals|round|rounds)\b", q)
    )
    if is_total:
        min_path, max_path = f"{comp_block}.total_raised_min", f"{comp_block}.total_raised_max"
    else:
        min_path, max_path = f"{deal_block}.{size_min_key}", f"{deal_block}.{size_max_key}"

    # --- amount: "between $1M and $10M", "over $5M", "under $2m" ---
    m = re.search(r"between\s+\$?([\d,.]+\s*\w*)\s+and\s+\$?([\d,.]+\s*\w*)", q)
    if m:
        lo, hi = _money(m.group(1)), _money(m.group(2))
        if lo:
            out[min_path] = lo
        if hi:
            out[max_path] = hi
    else:
        m = re.search(r"(?:over|more than|at least|above|>=?)\s+\$?([\d,.]+\s*\w*)", q)
        if m and _money(m.group(1)):
            out[min_path] = _money(m.group(1))
        m = re.search(r"(?:under|less than|below|at most|<=?)\s+\$?([\d,.]+\s*\w*)", q)
        if m and _money(m.group(1)):
            out[max_path] = _money(m.group(1))

    # --- dates: "since 2023", "in 2024", "after Jan 2023", "last 12 months" ---
    m = re.search(r"\b(?:since|after|from)\s+(\d{4})\b", q)
    if m:
        out[f"{deal_block}.{date_start_key}"] = f"{m.group(1)}-01-01"
    m = re.search(r"\bin\s+(\d{4})\b", q)
    if m:
        out[f"{deal_block}.{date_start_key}"] = f"{m.group(1)}-01-01"
        out[f"{deal_block}.{date_end_key}"] = f"{m.group(1)}-12-31"
    if re.search(r"last\s+12\s+months|past\s+year|trailing\s+12", q):
        try:
            t = date.fromisoformat(today)
        except ValueError:
            t = date.today()
        out[f"{deal_block}.{date_start_key}"] = (t - timedelta(days=365)).isoformat()

    # --- employee count buckets ---
    buckets = ["1-10", "11-50", "51-100", "101-250", "251-500", "501-1000",
               "1001-5000", "5001-10000", "10001+"]
    found = [b for b in buckets if b.replace("-", " to ") in q or b in q.replace(",", "")]
    if "1-10" in q or "under 10 employ" in q:
        found.append("1-10")
    if found:
        out[f"{comp_block}.employee_count"] = sorted(set(found))

    # --- ipo status ---
    ipo = []
    if re.search(r"\bpublic(ly)?\b", q):
        ipo.append("public")
    if re.search(r"\bprivate(ly)?\b", q):
        ipo.append("private")
    if ipo:
        out[f"{comp_block}.ipo_status"] = ipo

    # --- financing/round types (deterministic; not LLM-dependent) ---
    rounds = _extract_rounds(q)
    if rounds:
        out[_FIN_PATH[short]] = rounds

    # --- sort phrase ---
    for phrase, mapping in sorted(_SORT.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(phrase)}\b", q) and short in mapping:
            out["sort_by"] = mapping[short]
            break

    return out


_SERIES = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"]


def expand_open_ended_rounds(query: str) -> list[dict] | None:
    """Handle 'Series C+', 'Series C or higher', 'seed and up' etc. by expanding
    to that round AND every higher one. Returns financing_type objects, or None
    if no open-ended phrase is present. This is authoritative (overrides the
    model), since 'C+' means C,D,E,… not just C."""
    q = query.lower()
    higher = r"(?:\+|plus|or higher|or above|or later|or beyond|and up|and above|and higher|and later|onwards?|\band up\b)"

    # Series <letter> + / or higher
    m = re.search(rf"series\s+([a-m])\b\s*{higher}", q)
    if m:
        idx = _SERIES.index(m.group(1))
        return [{"type": f"SERIES_{L.upper()}"} for L in _SERIES[idx:]]

    # seed + / seed or higher  -> seed and every series
    if re.search(rf"\bseed\b\s*{higher}", q) and not re.search(r"pre[\s-]?seed", q):
        return [{"type": "SEED"}] + [{"type": f"SERIES_{L.upper()}"} for L in _SERIES]

    # pre-seed or higher -> pre-seed, seed, every series
    if re.search(rf"pre[\s-]?seed\b\s*{higher}", q):
        return ([{"type": "SEED", "pre": True}, {"type": "SEED"}]
                + [{"type": f"SERIES_{L.upper()}"} for L in _SERIES])

    return None


def _extract_rounds(q: str) -> list[str]:
    """Match round phrases (longest-first) with word boundaries; de-dupe."""
    found: list[str] = []
    for phrase in _ROUND_EXPANSIONS:
        if re.search(rf"\b{re.escape(phrase)}\b", q):
            found.append(phrase)
    for phrase in _ROUND_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", q):
            # skip 'seed' if 'pre-seed'/'pre seed' already matched at this spot
            if phrase == "seed" and re.search(r"\bpre[\s-]?seed\b", q):
                # keep seed too only if a standalone 'seed' exists beyond pre-seed
                if not re.search(r"(?<!pre[\s-])\bseed\b", q):
                    continue
            found.append(phrase)
    seen, out = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
