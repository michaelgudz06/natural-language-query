"""
Production unit tests for the deterministic pieces — the parts that must be
correct regardless of which model is used. All offline (no API keys), so they
run in CI. Run:  python3 tests/test_prod.py   (or pytest tests/)

Covers the bugs we actually hit and fixed:
  * Series C+ / "or higher" round expansion
  * Fundable's literal search (healthcare -> "health care" -> health-care)
  * spacing/hyphen-insensitive name matching
  * multi-location compilation
  * canonical resolver paths
  * the "no filters resolved" safety net
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from extractors import expand_open_ended_rounds            # noqa: E402
from resolvers import Candidate, _norm, _search_term, choose  # noqa: E402
from plan_types import ResolverTask, IntermediatePlan       # noqa: E402
from compiler import Compiler, _has_real_filter             # noqa: E402
from pathmap import canonical_path                          # noqa: E402

TODAY = "2026-07-22"


# ---- round expansion -------------------------------------------------------
def test_series_c_plus_expands_to_c_through_m():
    got = [o["type"] for o in expand_open_ended_rounds("Series C+ deals in fintech")]
    assert got == [f"SERIES_{c}" for c in "CDEFGHIJKLM"], got


def test_series_b_or_higher():
    got = [o["type"] for o in expand_open_ended_rounds("Series B or higher rounds")]
    assert got[0] == "SERIES_B" and got[-1] == "SERIES_M"


def test_seed_or_later_includes_seed_and_series():
    got = [o["type"] for o in expand_open_ended_rounds("seed or later stage deals")]
    assert got[0] == "SEED" and "SERIES_A" in got and got[-1] == "SERIES_M"


def test_plain_series_a_does_not_expand():
    assert expand_open_ended_rounds("Series A deals in AI") is None


# ---- Fundable literal search: alias rewrite + permalink pick ---------------
def test_search_term_rewrites_healthcare():
    assert _search_term(ResolverTask("industry", "healthcare", "company.industries")) == "health care"
    assert _search_term(ResolverTask("industry", "cybersecurity", "company.industries")) == "cyber security"
    assert _search_term(ResolverTask("location", "SF", "company.locations")).lower() == "san francisco"


def test_alias_picks_verified_permalink():
    task = ResolverTask("industry", "healthcare", "company.industries")
    cands = [Candidate("health-care", "Health Care", "INDUSTRY"),
             Candidate("home-health-care", "Home Health Care", "INDUSTRY")]
    out = choose(task, cands)
    assert out.status == "resolved" and out.chosen.value == "health-care"


def test_norm_ignores_spacing_and_hyphens():
    assert _norm("healthcare") == _norm("Health Care") == _norm("health-care")


def test_ambiguous_location_asks_for_clarification():
    task = ResolverTask("location", "Springfield", "company.locations")
    cands = [Candidate("springfield-massachusetts", "Springfield", "CITY"),
             Candidate("springfield-missouri", "Springfield", "CITY")]
    out = choose(task, cands)
    assert out.status == "needs_clarification" and len(out.candidates) == 2


# ---- multi-location + canonical paths --------------------------------------
def test_pathmap_canonical_paths():
    assert canonical_path("companies", ResolverTask("location", "SF", "")) == "company.locations"
    assert canonical_path("deals", ResolverTask("investor", "Sequoia", "")) == "investors.investor_ids"
    assert canonical_path("investors", ResolverTask("industry", "AI", "")) == "company_investments.industries"


def test_compiler_places_three_locations():
    from resolvers import ResolverOutcome
    plan = IntermediatePlan(route="POST /companies", extracted_filters={}, resolver_tasks=[], warnings=[], confidence=0.9)
    outcomes = [
        ResolverOutcome(ResolverTask("location", "San Francisco", "company.locations"), "resolved",
                        chosen=Candidate("san-francisco-california", "San Francisco", "CITY")),
        ResolverOutcome(ResolverTask("location", "New York", "company.locations"), "resolved",
                        chosen=Candidate("new-york-new-york", "New York", "CITY")),
        ResolverOutcome(ResolverTask("location", "Chicago", "company.locations"), "resolved",
                        chosen=Candidate("chicago-illinois", "Chicago", "CITY")),
    ]
    res = Compiler().compile(plan, outcomes, TODAY)
    assert res.valid
    assert res.body["company"]["locations"] == ["san-francisco-california", "new-york-new-york", "chicago-illinois"]


# ---- negation / exclusion (API is include-only) ----------------------------
def test_excluded_locations_are_not_filtered_and_warned():
    from planner import flat_to_plan
    flat = {"route": "POST /companies", "reasoning": "", "locations": [],
            "industries": ["fintech"], "excluded_locations": ["San Francisco", "New York"],
            "excluded_industries": [], "investors": [], "companies": [], "people": [],
            "round_phrases": ["series b"], "search_query": None, "sort_hint": None,
            "confidence": 0.9, "warnings": []}
    plan = flat_to_plan(flat)
    # the excluded places must NOT become positive location filters
    assert [t.query for t in plan.resolver_tasks if t.type == "location"] == []
    # they go into excluded_tasks (resolved + applied client-side downstream)
    excl = [t.query for t in plan.excluded_tasks]
    assert "San Francisco" in excl and "New York" in excl


# ---- /deals has no company.search_query ------------------------------------
def test_deals_route_never_emits_company_search_query():
    from planner import flat_to_plan
    flat = {"route": "POST /deals", "reasoning": "", "locations": [], "industries": [],
            "excluded_locations": [], "excluded_industries": [], "investors": [], "companies": [],
            "people": [], "round_phrases": ["seed"], "search_query": "companies with 11-50 employees",
            "sort_hint": None, "confidence": 0.9, "warnings": []}
    plan = flat_to_plan(flat)
    assert "company.search_query" not in plan.extracted_filters
    # and it compiles to a valid /deals body
    from compiler import Compiler
    res = Compiler().compile(plan, [], TODAY)
    assert res.valid, res.errors


# ---- person-side filters (POST /people) ------------------------------------
def _people_flat(**over):
    base = {"route": "POST /people", "reasoning": "", "locations": [], "industries": [],
            "excluded_locations": [], "excluded_industries": [], "investors": [],
            "companies": [], "people": [], "person_roles": [], "job_titles": [],
            "schools": [], "past_employers": [], "round_phrases": [],
            "search_query": None, "sort_hint": None, "confidence": 0.9, "warnings": []}
    base.update(over)
    return base


def test_person_roles_and_titles_extracted():
    from planner import flat_to_plan
    p = flat_to_plan(_people_flat(person_roles=["Founder"], job_titles=["CTO"]))
    assert p.extracted_filters["person.roles"] == ["founder"]
    assert p.extracted_filters["person.job_titles"] == ["cto"]


def test_person_schools_and_past_employers_slugged():
    from planner import flat_to_plan
    p = flat_to_plan(_people_flat(schools=["Stanford University"], past_employers=["Google"]))
    assert p.extracted_filters["person.education_schools"] == ["stanford-university"]
    assert p.extracted_filters["person.linkedin_companies"] == ["google"]


def test_person_roles_enum_guarded_and_aliased():
    from planner import flat_to_plan
    # 'co-founder' maps to founder; a bogus role is dropped (roles is a strict enum)
    p = flat_to_plan(_people_flat(person_roles=["co-founder", "wizard"]))
    assert p.extracted_filters["person.roles"] == ["founder"]


def test_person_filters_only_on_people_route():
    from planner import flat_to_plan
    flat = _people_flat(person_roles=["founder"]); flat["route"] = "POST /companies"
    p = flat_to_plan(flat)
    assert not any(k.startswith("person.") for k in p.extracted_filters)


def test_people_body_compiles_valid():
    from planner import flat_to_plan
    from compiler import Compiler
    p = flat_to_plan(_people_flat(person_roles=["founder"], past_employers=["Google"]))
    res = Compiler().compile(p, [], TODAY)
    assert res.valid, res.errors


# ---- safety net ------------------------------------------------------------
def test_filterless_query_is_flagged():
    plan = IntermediatePlan(route="POST /companies", extracted_filters={}, resolver_tasks=[], warnings=[], confidence=0.9)
    res = Compiler().compile(plan, [], TODAY)
    assert not _has_real_filter(res.body)
    assert res.confidence <= 0.3
    assert any("match everything" in w for w in res.warnings)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} production unit tests passed.")
