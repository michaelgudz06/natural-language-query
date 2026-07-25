"""
Sprint 3: deterministic resolvers.

Turns the planner's resolver_tasks (human display names) into canonical API
values (permalinks for locations/industries, UUIDs for named entities). This is
plain code — the LLM plans the lookups, code executes them.

Two backends behind one interface:
  * HttpResolver  — real GET calls to the Fundable search endpoints.
  * MockResolver  — offline fixture data so the pipeline, tests, and demo run
                    with no network / no API key.

Key behaviors required by the spec:
  * Independent lookups run in PARALLEL (§6) via a thread pool.
  * Locations/industries/entities are NEVER guessed — only what a lookup
    returns is used (§8).
  * When a lookup is ambiguous we either auto-pick a confident match or emit a
    needs_clarification outcome (§9).

NOTE (blocker): the Fundable MCP connector in this workspace is unauthorized and
the public docs don't publish the API host, so live HTTP could not be exercised
here. HttpResolver is written to the documented contract; confirm base_url +
auth, then flip USE_HTTP on. Everything else runs against MockResolver.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# macOS Python.org builds ship no system CA bundle; use certifi's when available
# so urllib can verify the Fundable API's TLS cert.
try:
    import certifi
    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CTX = None
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from plan_types import ResolverTask

Status = Literal["resolved", "needs_clarification", "not_found"]

_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(_HERE, "..", "config", "aliases.yaml")) as _af:
        _ALIASES = yaml.safe_load(_af) or {}
except Exception:  # noqa: BLE001
    _ALIASES = {}


def _norm(s: str) -> str:
    # strip ALL non-alphanumerics (incl. spaces/hyphens) so "healthcare",
    # "health care", and "health-care" all compare equal.
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# normalized-key views of the alias tables
_LOC_ALIAS = {_norm(k): v for k, v in (_ALIASES.get("locations") or {}).items()}
_IND_ALIAS = {_norm(k): v for k, v in (_ALIASES.get("industry_aliases") or {}).items()}
# rich location aliases: {alias -> {search, permalink}} (nicknames, abbreviations,
# metros) — search term steers Fundable's literal lookup, permalink pins the pick.
_LOC_ALIAS2 = {_norm(k): v for k, v in (_ALIASES.get("location_aliases") or {}).items()}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    value: str            # permalink or UUID
    label: str            # display name
    kind: str | None = None   # CITY/STATE/REGION/COUNTRY or INDUSTRY/SUPER_CATEGORY
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolverOutcome:
    task: ResolverTask
    status: Status
    chosen: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_path": self.task.target_path,
            "query": self.task.query,
            "status": self.status,
            "chosen": None if not self.chosen else {"value": self.chosen.value, "label": self.chosen.label, "kind": self.chosen.kind},
            "candidates": [{"value": c.value, "label": c.label, "kind": c.kind} for c in self.candidates],
        }


# Location abbreviations the search endpoint doesn't understand — expand before
# the lookup (e.g. /location/search?name=SF returns nothing; "san francisco" works).
_LOC_SEARCH_EXPAND = {
    "sf": "san francisco", "sfo": "san francisco", "nyc": "new york",
    "la": "los angeles", "lax": "los angeles",
    # "America"/"USA" literally matches a town named "America, Alabama"; steer the
    # search to the country so it resolves to United States, not a hamlet.
    "america": "united states", "usa": "united states", "us": "united states",
    "u s a": "united states", "the states": "united states",
}


def _search_term(task: ResolverTask) -> str:
    """The string to actually send to the search endpoint (Fundable is literal:
    it has no synonym handling, so 'healthcare' returns nothing — send the exact
    phrase it indexes)."""
    n = _norm(task.query)
    if task.type == "industry" and n in _IND_ALIAS:
        return _IND_ALIAS[n]["search"]
    if task.type == "location":
        if n in _LOC_ALIAS2:
            return _LOC_ALIAS2[n]["search"]
        if n in _LOC_SEARCH_EXPAND:
            return _LOC_SEARCH_EXPAND[n]
    return task.query


# prominence when names tie: a country/region beats a state beats a hamlet.
_TYPE_PRIORITY = {"COUNTRY": 4, "REGION": 4, "STATE": 3, "CITY": 1,
                  "INDUSTRY": 2, "SUPER_CATEGORY": 1}


def _distinguish(cands: list[Candidate]) -> list[Candidate]:
    """When several options share a label, append their permalink so a human can
    actually tell them apart in the clarification prompt."""
    from collections import Counter
    names = Counter(c.label for c in cands)
    out = []
    for c in cands:
        if names[c.label] > 1:
            out.append(Candidate(c.value, f"{c.label} · {c.value}", c.kind, c.raw))
        else:
            out.append(c)
    return out


def _alias_permalink(task: ResolverTask) -> str | None:
    n = _norm(task.query)
    if task.type == "location":
        if n in _LOC_ALIAS2:
            return _LOC_ALIAS2[n]["permalink"]
        return _LOC_ALIAS.get(n)
    if task.type == "industry":
        entry = _IND_ALIAS.get(n)
        return entry["permalink"] if entry else None
    return None


# ---------------------------------------------------------------------------
# Disambiguation policy (shared by both backends). No API score exists, so this
# is alias + name-match + result-count based (see docs/GAPS.md #1).
# ---------------------------------------------------------------------------
def choose(task: ResolverTask, candidates: list[Candidate]) -> ResolverOutcome:
    if not candidates:
        return ResolverOutcome(task, "not_found", candidates=[])

    # hint filter; for industries default to preferring the specific INDUSTRY
    # over the broader SUPER_CATEGORY when the planner gave no hint.
    pool = candidates
    hint = task.hint or ("INDUSTRY" if task.type == "industry" else None)
    if hint:
        hinted = [c for c in candidates if (c.kind or "").upper() == hint.upper()]
        if hinted:
            pool = hinted

    # 1) Curated alias for a well-known entity — but ONLY if that exact permalink
    #    is actually present in the live results (never fabricate a permalink).
    ap = _alias_permalink(task)
    if ap:
        match = next((c for c in pool if c.value == ap), None) or \
                next((c for c in candidates if c.value == ap), None)
        if match:
            return ResolverOutcome(task, "resolved", chosen=match, candidates=pool)

    if len(pool) == 1:
        return ResolverOutcome(task, "resolved", chosen=pool[0], candidates=pool)

    # 2) Exact normalized-name match. If several tie (e.g. the country "Canada"
    #    AND a hamlet "Canada, Kentucky"), prefer the more prominent TYPE — a
    #    country/state beats a village — so we don't ask the user to choose between
    #    two identical-looking "Canada" chips.
    q = _norm(task.query)
    exact = [c for c in pool if _norm(c.label) == q]
    if exact:
        exact.sort(key=lambda c: _TYPE_PRIORITY.get((c.kind or "").upper(), 0), reverse=True)
        top = _TYPE_PRIORITY.get((exact[0].kind or "").upper(), 0)
        top_tier = [c for c in exact if _TYPE_PRIORITY.get((c.kind or "").upper(), 0) == top]
        if len(top_tier) == 1:
            return ResolverOutcome(task, "resolved", chosen=top_tier[0], candidates=pool)
        # genuine tie at the same level (e.g. several cities named "New York") ->
        # ask, but relabel so the options are actually distinguishable.
        return ResolverOutcome(task, "needs_clarification", candidates=_distinguish(top_tier)[:5])

    # 3) No exact match at all — ask the user (options already differ by name).
    return ResolverOutcome(task, "needs_clarification", candidates=_distinguish(pool)[:5])


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class BaseResolver:
    def search(self, task: ResolverTask) -> list[Candidate]:  # -> raw candidates
        raise NotImplementedError

    def resolve_one(self, task: ResolverTask) -> ResolverOutcome:
        return choose(task, self.search(task))

    def resolve_all(self, tasks: list[ResolverTask], max_workers: int = 8) -> list[ResolverOutcome]:
        """Run independent lookups concurrently (spec §6)."""
        if not tasks:
            return []
        with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as ex:
            return list(ex.map(self.resolve_one, tasks))


class HttpResolver(BaseResolver):
    # type -> (path, response_list_key, value_field, kind_field)  [verified live]
    _ENDPOINTS = {
        "location": ("/location/search", "locations", "permalink", "location_type"),
        "industry": ("/industry/search", "industries", "permalink", "industry_type"),
        "company": ("/company/search", "companies", "id", None),
        "investor": ("/investor/search", "investors", "id", None),
        "people": ("/person/search", "people", "id", None),   # NB: /person, not /people
    }

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.environ.get("FUNDABLE_BASE_URL", "https://www.tryfundable.ai/api/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("FUNDABLE_API_KEY", "")

    def search(self, task: ResolverTask) -> list[Candidate]:
        path, list_key, value_key, kind_key = self._ENDPOINTS[task.type]
        params = {"name": _search_term(task)}
        if task.hint and task.type in ("location", "industry"):
            params["type"] = task.hint
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as r:  # noqa: S310
                payload = json.loads(r.read().decode())
        except Exception:  # noqa: BLE001 — a failed lookup must not crash the pipeline;
            return []       # it degrades to "not_found" -> a warning, filter dropped.
        rows = payload.get("data", {}).get(list_key, [])
        return [
            Candidate(
                value=row[value_key],
                label=row.get("name", task.query),
                kind=(row.get(kind_key) if kind_key else None),
                raw=row,
            )
            for row in rows
        ]


class MockResolver(BaseResolver):
    """Offline fixtures. Extend freely; used by tests + demo + Sprint 4 harness."""

    LOCATIONS = {
        "san francisco": [("san-francisco-california", "San Francisco", "CITY"),
                          ("san-francisco-bay-area", "San Francisco Bay Area", "REGION")],
        "sf": [("san-francisco-california", "San Francisco", "CITY")],
        "new york": [("new-york-new-york", "New York", "CITY"),
                     ("new-york", "New York", "STATE")],
        "nyc": [("new-york-new-york", "New York", "CITY")],
        "london": [("london-england", "London", "CITY")],
        "boston": [("boston-massachusetts", "Boston", "CITY")],
        "austin": [("austin-texas", "Austin", "CITY")],
        "berlin": [("berlin-germany", "Berlin", "CITY")],
        "california": [("california", "California", "STATE")],
        "united states": [("united-states", "United States", "COUNTRY")],
    }
    INDUSTRIES = {
        "fintech": [("fintech-e067", "Fintech", "INDUSTRY")],
        "financial services": [("financial-services-d7b6", "Financial Services", "SUPER_CATEGORY")],
        "ai": [("artificial-intelligence", "Artificial Intelligence", "INDUSTRY")],
        "artificial intelligence": [("artificial-intelligence", "Artificial Intelligence", "INDUSTRY")],
        "healthcare": [("health-care", "Health Care", "INDUSTRY")],
        "climate": [("climate-tech", "Climate Tech", "INDUSTRY")],
        "biotech": [("biotechnology", "Biotechnology", "INDUSTRY")],
        "saas": [("software-as-a-service", "SaaS", "INDUSTRY")],
        "crypto": [("cryptocurrency", "Cryptocurrency", "INDUSTRY")],
    }
    ENTITIES = {  # investor/company/people named entities -> pseudo UUIDs
        "sequoia": [("11111111-1111-1111-1111-111111111111", "Sequoia Capital", None)],
        "a16z": [("22222222-2222-2222-2222-222222222222", "Andreessen Horowitz", None)],
        "andreessen": [("22222222-2222-2222-2222-222222222222", "Andreessen Horowitz", None)],
        "accel": [("33333333-3333-3333-3333-333333333333", "Accel", None),
                  ("34343434-3434-3434-3434-343434343434", "Accel-KKR", None)],
        "benchmark": [("44444444-4444-4444-4444-444444444444", "Benchmark", None)],
        "founders fund": [("55555555-5555-5555-5555-555555555555", "Founders Fund", None)],
    }

    def search(self, task: ResolverTask) -> list[Candidate]:
        key = _norm(task.query)
        if task.type == "location":
            rows = self.LOCATIONS.get(key, [])
        elif task.type == "industry":
            rows = self.INDUSTRIES.get(key, [])
        else:
            rows = self.ENTITIES.get(key, [])
        return [Candidate(value=v, label=l, kind=k) for (v, l, k) in rows]


class OverrideResolver(BaseResolver):
    """Wraps a resolver and force-resolves specific (type, query) tasks to a
    permalink the USER picked in a clarification prompt. Everything else delegates
    to the inner resolver. This is how the demo's clickable follow-ups work."""

    def __init__(self, inner: BaseResolver, overrides: list[dict]):
        self.inner = inner
        # {(type, normalized query): (permalink, label)}
        self.map = {(o["type"], _norm(o["query"])): (o["permalink"], o.get("label", o["permalink"]))
                    for o in (overrides or [])}

    def resolve_one(self, task: ResolverTask) -> ResolverOutcome:
        pin = self.map.get((task.type, _norm(task.query)))
        if pin:
            return ResolverOutcome(task, "resolved", chosen=Candidate(pin[0], pin[1], None))
        return self.inner.resolve_one(task)


# Flip to HttpResolver once base_url + auth are confirmed.
USE_HTTP = bool(os.environ.get("FUNDABLE_API_KEY"))


def get_resolver() -> BaseResolver:
    return HttpResolver() if USE_HTTP else MockResolver()
