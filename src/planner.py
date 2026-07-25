"""
Sprint 2 deliverable: Phase-1 planner.

Two implementations behind one interface:

  * LLMPlanner       — calls an OpenAI/OpenRouter structured-output model using
                       the prompts + schema from config/planner.yaml. This is the
                       production path.
  * HeuristicPlanner — a dependency-free, offline rule-based planner. It lets you
                       run the pipeline, the tests, and a demo WITHOUT any API
                       key, and it doubles as the deterministic sanity baseline
                       the bakeoff (Sprint 5) measures the models against.

Both return an IntermediatePlan. Neither ever emits a resolved permalink/UUID —
that is Phase 2's job.
"""
from __future__ import annotations

import os
import re
from typing import Any, Protocol

import yaml

from plan_types import (
    PLANNER_OUTPUT_SCHEMA,
    IntermediatePlan,
    ResolverTask,
)

# where financing_types / the company block live per route (for the flat adapter)
_ROUTE_SHORT = {"POST /deals": "deals", "POST /companies": "companies",
                "POST /investors": "investors", "POST /people": "people"}
_FIN_PATH = {"deals": "deal.financing_types", "companies": "latest_deal.financing_types",
             "investors": "company_investments.financing_types",
             "people": "company.latest_deal.financing_types"}
_COMPANY_BLOCK = {"deals": "company", "companies": "company",
                  "investors": "company_investments", "people": "company"}


# person.roles is a strict enum in the API — anything else belongs in job_titles.
_VALID_ROLES = {"founder", "ceo", "key_person"}
_ROLE_ALIASES = {"co-founder": "founder", "cofounder": "founder", "founders": "founder",
                 "ceos": "ceo", "chief executive officer": "ceo",
                 "executive": "key_person", "exec": "key_person", "operator": "key_person"}


def _slug(v: str) -> str:
    """'VP Engineering' -> 'vp-engineering'; 'Stanford University' -> 'stanford-university'."""
    return re.sub(r"[^a-z0-9]+", "-", v.strip().lower()).strip("-")


def flat_to_plan(flat: dict[str, Any]) -> IntermediatePlan:
    """Convert the model's flat output into an IntermediatePlan. Resolver target
    paths are left blank here — pathmap.normalize_tasks fills them canonically."""
    route = flat["route"]
    short = _ROUTE_SHORT[route]
    tasks: list[ResolverTask] = []
    for rtype, key in (("location", "locations"), ("industry", "industries"),
                       ("investor", "investors"), ("company", "companies"),
                       ("people", "people")):
        for name in flat.get(key) or []:
            if isinstance(name, str) and name.strip():
                tasks.append(ResolverTask(rtype, name.strip(), ""))
    filters: dict[str, Any] = {}
    rounds = [r for r in (flat.get("round_phrases") or []) if isinstance(r, str)]
    if rounds:
        filters[_FIN_PATH[short]] = rounds
    sq = flat.get("search_query")
    # NB: POST /deals' company block has NO search_query field, so never place one
    # there (it would make the body invalid). Other routes support it.
    if isinstance(sq, str) and sq.strip() and short != "deals":
        filters[f"{_COMPANY_BLOCK[short]}.search_query"] = sq.strip()

    # --- person-side filters: only valid on POST /people ---
    if short == "people":
        roles = [r.lower().strip() for r in (flat.get("person_roles") or []) if isinstance(r, str)]
        roles = [_ROLE_ALIASES.get(r, r) for r in roles]
        roles = [r for r in dict.fromkeys(roles) if r in _VALID_ROLES]   # enum-guard
        if roles:
            filters["person.roles"] = roles
        for key, path in (("job_titles", "person.job_titles"),
                          ("schools", "person.education_schools"),
                          ("past_employers", "person.linkedin_companies")):
            vals = [_slug(v) for v in (flat.get(key) or []) if isinstance(v, str) and v.strip()]
            vals = list(dict.fromkeys(vals))
            if vals:
                filters[path] = vals

    warnings = [w for w in (flat.get("warnings") or []) if isinstance(w, str)]
    # Excluded terms never enter the positive filters (that would return the exact
    # opposite). They're resolved to permalinks and applied CLIENT-SIDE downstream
    # (count by subtraction + page post-filter), since Fundable is include-only.
    excluded_tasks: list[ResolverTask] = []
    for name in (flat.get("excluded_locations") or []):
        if isinstance(name, str) and name.strip():
            excluded_tasks.append(ResolverTask("location", name.strip(), "__exclude__"))
    for name in (flat.get("excluded_industries") or []):
        if isinstance(name, str) and name.strip():
            excluded_tasks.append(ResolverTask("industry", name.strip(), "__exclude__"))

    return IntermediatePlan(
        route=route,
        extracted_filters=filters,
        resolver_tasks=tasks,
        warnings=warnings,
        confidence=float(flat.get("confidence", 0.0)),
        reasoning=str(flat.get("reasoning") or ""),
        excluded_tasks=excluded_tasks,
    )

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_HERE, "..", "config", "planner.yaml")


def load_config(path: str = _CONFIG_PATH) -> dict[str, Any]:
    with open(path, "r") as f:
        return yaml.safe_load(f)


class Planner(Protocol):
    def plan(self, query: str, today: str) -> IntermediatePlan: ...


# ---------------------------------------------------------------------------
# Production planner (LLM). Kept thin: prompt assembly + structured-output call.
# ---------------------------------------------------------------------------
class LLMPlanner:
    def __init__(self, config: dict[str, Any] | None = None, tier: str = "primary"):
        self.config = config or load_config()
        self.tier = tier

    def plan(self, query: str, today: str) -> IntermediatePlan:
        model_cfg = self.config["models"][self.tier]
        provider = model_cfg["provider"]
        messages = [
            {"role": "system", "content": self.config["prompts"]["system"]},
            {"role": "user", "content": self.config["prompts"]["user_template"].format(query=query, today=today)},
        ]
        raw = self._call(provider, model_cfg, messages)
        return flat_to_plan(raw)

    # provider -> (base_url, api-key env var). All are OpenAI-compatible endpoints.
    _PROVIDERS = {
        "openai": (None, "OPENAI_API_KEY"),
        "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/",
                   "GOOGLE_GENERATIVE_AI_API_KEY"),
    }

    def _call(self, provider: str, model_cfg: dict, messages: list[dict]) -> dict:
        """
        Structured-output call against any OpenAI-compatible endpoint (OpenAI,
        OpenRouter, and Gemini via Google's OpenAI-compat layer). Requires the
        `openai` package and the provider's API key in the environment.
        """
        import json

        from openai import OpenAI  # imported lazily so offline use needs no dep

        base_url, key_env = self._PROVIDERS.get(provider, (None, "OPENAI_API_KEY"))
        client = OpenAI(base_url=base_url, api_key=os.environ[key_env])

        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": self.config.get("output_schema_name", "planner_output"),
                "schema": PLANNER_OUTPUT_SCHEMA,
                "strict": True,
            },
        }
        try:
            resp = client.chat.completions.create(
                model=model_cfg["name"],
                temperature=model_cfg.get("temperature", 0),
                messages=messages,
                response_format=response_format,
            )
        except Exception:  # noqa: BLE001
            # Some models/endpoints reject strict json_schema; retry with json_object.
            resp = client.chat.completions.create(
                model=model_cfg["name"],
                temperature=model_cfg.get("temperature", 0),
                messages=messages,
                response_format={"type": "json_object"},
            )
        content = resp.choices[0].message.content
        # capture token usage for the bakeoff, if the endpoint reports it
        self.last_usage = getattr(resp, "usage", None)
        return json.loads(content)


# ---------------------------------------------------------------------------
# Offline heuristic planner — no network, no keys. Good enough for obvious
# queries; intentionally conservative (lower confidence) on hard ones so the
# router would escalate to the LLM in production.
# ---------------------------------------------------------------------------
_DEAL_WORDS = ("deal", "deals", "round", "rounds", "raise", "raised", "raising",
               "financing", "financings", "funding round", "funding rounds")
_INVESTOR_WORDS = ("fund", "funds", "vc", "vcs", "vc firm", "investor", "investors",
                   "investment firm", "firm", "firms")
_PEOPLE_WORDS = ("founder", "founders", "ceo", "ceos", "cto", "ctos", "exec", "execs",
                 "executive", "operator", "angel", "angels", "partner", "partners",
                 "people", "person")
_COMPANY_WORDS = ("company", "companies", "startup", "startups", "business",
                  "businesses", "organization", "organizations", "org", "orgs")


def _has_word(q: str, words) -> bool:
    """Whole-word match so 'fund' does not fire inside 'funding'."""
    return any(re.search(rf"\b{re.escape(w)}\b", q) for w in words)

_ROUND_PATTERNS = {
    r"\bpre-?seed\b": "pre-seed",
    r"\bseed\b": "seed",
    r"\bseries\s+a\b": "series a",
    r"\bseries\s+b\b": "series b",
    r"\bseries\s+c\b": "series c",
    r"\bseries\s+d\b": "series d",
}


class HeuristicPlanner:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or load_config()

    def plan(self, query: str, today: str) -> IntermediatePlan:
        q = query.lower()
        route, route_conf = self._route(q)
        short = route.split("/")[-1]                 # deals/companies/investors/people
        company_block = "company_investments" if short == "investors" else "company"

        filters: dict[str, Any] = {}
        tasks: list[ResolverTask] = []
        warnings: list[str] = []

        # --- round / financing phrases (deterministic; named, not resolved) ---
        rounds = []
        for pat, phrase in _ROUND_PATTERNS.items():
            if re.search(pat, q):
                rounds.append(phrase)
        if rounds:
            deal_key = {
                "deals": "deal.financing_types",
                "companies": "latest_deal.financing_types",
                "investors": "company_investments.financing_types",
                "people": "company.latest_deal.financing_types",
            }[short]
            filters[deal_key] = rounds

        # --- named investor (very rough NER for the demo baseline) ---------
        # Placement differs by route: deals/companies -> investors.investor_ids;
        # people -> investor.ids (firm the person is at). On /investors the named
        # firm is genuinely ambiguous (the firm itself? a co-investor? a portfolio
        # owner?), so the baseline leaves it for the LLM rather than guess a field.
        _firm_path = {"deals": "investors.investor_ids", "companies": "investors.investor_ids",
                      "people": "investor.ids"}
        for firm in ("sequoia", "a16z", "andreessen", "accel", "benchmark", "founders fund"):
            if firm in q and short in _firm_path:
                tasks.append(ResolverTask("investor", firm.title(), _firm_path[short]))

        # --- locations (naive: capitalized multiword after "in") -----------
        for loc in self._extract_locations(query):
            tasks.append(ResolverTask("location", loc, f"{company_block}.locations", hint="CITY"))

        # --- industries (small keyword lexicon for the baseline) -----------
        for term in ("fintech", "ai", "artificial intelligence", "healthcare", "climate", "biotech", "saas"):
            if re.search(rf"\b{re.escape(term)}\b", q):
                tasks.append(ResolverTask("industry", term, f"{company_block}.industries", hint="INDUSTRY"))

        # --- sort phrase ---------------------------------------------------
        for phrase, mapping in self.config["sort_phrase_map"].items():
            if phrase in q and short in mapping:
                filters["sort_by"] = mapping[short]
                break

        if route_conf < self.config["thresholds"]["route_clarify_below"]:
            warnings.append(
                "Route is ambiguous between the object and its funding rounds — "
                "consider asking the user which they meant."
            )

        return IntermediatePlan(
            route=route,
            extracted_filters=filters,
            resolver_tasks=tasks,
            warnings=warnings,
            confidence=round(route_conf, 2),
        )

    # -- helpers ------------------------------------------------------------
    def _route(self, q: str) -> tuple[str, float]:
        # Head-noun (the object the user wants back) beats a funding verb:
        # "founders who raised Series A" -> people, not deals. The LLM makes
        # this call properly; the baseline uses a fixed precedence.
        has_person = _has_word(q, _PEOPLE_WORDS)
        has_investor = _has_word(q, _INVESTOR_WORDS)
        has_deal = _has_word(q, _DEAL_WORDS)
        has_company = _has_word(q, _COMPANY_WORDS)

        if has_person:
            return "POST /people", 0.85 if not has_deal else 0.7
        if has_investor:
            return "POST /investors", 0.85
        if has_deal:
            return "POST /deals", 0.9
        if has_company:
            return "POST /companies", 0.8
        # default per spec §4
        return "POST /companies", 0.5

    def _extract_locations(self, query: str) -> list[str]:
        # Capture "in <Proper Noun[, Proper Noun]>" and "based in <...>".
        out: list[str] = []
        # Grab the run after a trigger word: capitalized tokens joined by
        # connectors (or / and / comma), e.g. "San Francisco, New York or London".
        for m in re.finditer(
            r"\b(?:based in|in|from)\s+((?:[A-Z][\w.]+|\s|,|\bor\b|\band\b)+)", query
        ):
            chunk = m.group(1)
            # split "San Francisco or New York" / "SF, NY and London"
            for part in re.split(r"\s+(?:or|and)\s+|,\s*", chunk):
                part = part.strip().strip(".").strip()
                if part and part.lower() not in ("the", "a"):
                    out.append(part)
        # de-dupe preserving order
        seen, res = set(), []
        for p in out:
            if p.lower() not in seen:
                seen.add(p.lower())
                res.append(p)
        return res


def openrouter_planner(model: str, config: dict | None = None) -> "LLMPlanner":
    """Build an LLMPlanner pinned to a specific OpenRouter model id."""
    cfg = dict(config or load_config())
    cfg["models"] = dict(cfg["models"])
    cfg["models"]["primary"] = {"provider": "openrouter", "name": model,
                                "temperature": 0, "structured_output": True}
    return LLMPlanner(cfg, tier="primary")


def get_planner(config: dict | None = None) -> Planner:
    """Pick the planner: LLM if a key is present, else the offline baseline."""
    cfg = config or load_config()
    if os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY"):
        cfg = _with_gemini_primary(cfg)
        return LLMPlanner(cfg)
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY"):
        return LLMPlanner(cfg)
    return HeuristicPlanner(cfg)


def _with_gemini_primary(cfg: dict) -> dict:
    """Point the primary tier at Gemini using env-provided model id."""
    cfg = dict(cfg)
    cfg["models"] = dict(cfg["models"])
    cfg["models"]["primary"] = {
        "provider": "gemini",
        "name": os.environ.get("GOOGLE_MODEL", "gemini-3-flash-preview"),
        "temperature": 0,
        "structured_output": True,
    }
    return cfg
