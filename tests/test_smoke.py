"""
Smoke tests proving Sprint 1 (schema validation) and Sprint 2 (planner) work
offline with no API keys. Run:  python -m pytest tests/ -q   (from repo root)
or:  python tests/test_smoke.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from schema import SchemaMap          # noqa: E402
from planner import HeuristicPlanner  # noqa: E402

TODAY = "2026-07-22"


def test_schema_accepts_valid_deals_body():
    sm = SchemaMap.load()
    body = {
        "deal": {"financing_types": [{"type": "SEED", "pre": True}, {"type": "SERIES_A"}]},
        "company": {"locations": ["san-francisco-california"], "industries": ["fintech-e067"]},
        "page_size": 25,
        "sort_by": "most_recent_deal",
    }
    res = sm.validate("POST /deals", body)
    assert res.ok, res.errors


def test_schema_rejects_unknown_field():
    sm = SchemaMap.load()
    res = sm.validate("POST /deals", {"deal": {"bogus_field": 1}})
    assert not res.ok
    assert any("unsupported field" in e for e in res.errors)


def test_schema_rejects_bad_financing_enum():
    sm = SchemaMap.load()
    res = sm.validate("POST /deals", {"deal": {"financing_types": [{"type": "SERIES_Z"}]}})
    assert not res.ok
    assert any("invalid financing_type" in e for e in res.errors)


def test_schema_rejects_search_query_plus_industries():
    sm = SchemaMap.load()
    body = {"company": {"search_query": "ai copilots", "industries": ["health-care"]}}
    res = sm.validate("POST /companies", body)
    assert not res.ok
    assert any("search_query cannot be combined" in e for e in res.errors)


def test_schema_rejects_min_relevance_without_search_query():
    sm = SchemaMap.load()
    res = sm.validate("POST /companies", {"company": {"min_relevance": 0.5}})
    assert not res.ok
    assert any("min_relevance is only valid" in e for e in res.errors)


def test_schema_rejects_bad_sort():
    sm = SchemaMap.load()
    res = sm.validate("POST /companies", {"sort_by": "most_recent_deal"})  # deals-only sort
    assert not res.ok


def test_schema_people_ipo_status_extended():
    sm = SchemaMap.load()
    # 'acquired' is valid for people.company but NOT for companies.company
    ok = sm.validate("POST /people", {"company": {"ipo_status": ["acquired"]}})
    assert ok.ok, ok.errors
    bad = sm.validate("POST /companies", {"company": {"ipo_status": ["acquired"]}})
    assert not bad.ok


def test_planner_routes_deals():
    p = HeuristicPlanner()
    plan = p.plan("Find pre-seed and seed stage deals in San Francisco or New York", TODAY)
    assert plan.route == "POST /deals"
    locs = [t.query for t in plan.resolver_tasks if t.type == "location"]
    assert "San Francisco" in locs and "New York" in locs
    assert "deal.financing_types" in plan.extracted_filters


def test_planner_defaults_startups_to_companies():
    p = HeuristicPlanner()
    plan = p.plan("Find seed-stage AI startups in SF", TODAY)
    assert plan.route == "POST /companies"


def test_planner_routes_people_and_investors():
    p = HeuristicPlanner()
    assert p.plan("Which founders raised Series A in fintech", TODAY).route == "POST /people"
    assert p.plan("Most active VC firms in climate", TODAY).route == "POST /investors"


def test_planner_never_emits_permalinks():
    """The planner must emit display names, never guessed slugs."""
    p = HeuristicPlanner()
    plan = p.plan("seed fintech deals in San Francisco", TODAY)
    for t in plan.resolver_tasks:
        assert "-" not in t.query or t.query[0].isupper(), t.query  # no kebab slugs


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  PASS  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} smoke tests passed.")
