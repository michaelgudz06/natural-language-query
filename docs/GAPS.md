# Gaps, Risks & Open Questions (flagged from Sprint 1 build)

Things worth deciding before Sprint 3+, several of which contradict or under-specify
the brief. Ordered by how much they affect the design.

## 1. Search endpoints expose NO score AND no prominence ordering  ⚠️⚠️ CONFIRMED LIVE
Worse than the spec assumed. Verified against the live API on 2026-07-22:
- Both `GET /location/search` and `GET /industry/search` return only
  `{permalink, name, type}` — **no score, no relevance number** (spec §7/§9/§17 assume
  "API result confidence where available"; it is not available).
- **Results are not even ordered by prominence.** Real responses:
  - `name=new york` → returns **`new-york-chiapas` (Mexico) FIRST**, then
    `new-york-new-york` (NYC), then 5 more — all literally named "New York".
  - `name=london` → returns `city-of-london-essex`, `london-ontario`,
    `london-kentucky` **before** `london-england`.
  - `name=san francisco` → 3 different cities all named exactly "San Francisco".
- **Consequence:** there is no signal in the API to auto-pick the intended major
  city. Naively trusting rank-0 or "exact name match" silently returns the WRONG
  place (we hit exactly this: a deal query resolved to New York, Chiapas).
- **What we shipped:** (a) clarify on ambiguous homonyms — list candidates, let the
  user/app choose (spec §9); (b) a curated alias table (`config/aliases.yaml`) that
  maps well-known names → the obvious permalink, applied **only when that permalink
  actually appears in the live results** (never fabricated). Result: New York→NYC,
  London→London England resolve automatically; Springfield still clarifies.
- **Decision for Jacob:** grow the alias table (cheap, high-leverage) vs. build a
  client-side prominence scorer vs. always-clarify. Alias table is the pragmatic win.

## 1b. Industry/location search is LITERAL — common shorthand returns nothing  ⚠️ CONFIRMED LIVE
Fundable's `/industry/search` and `/location/search` do exact fuzzy-name matching with
no synonym handling. Everyday terms people type return **zero results**, so the filter
silently drops and the query matches everything:

| user types | Fundable returns | correct term / permalink |
|-----------|------------------|--------------------------|
| `healthcare` | **nothing** | `health care` → `health-care` |
| `cybersecurity` | **nothing** | `cyber security` → `cyber-security` |
| `ecommerce` | **nothing** | `e-commerce` → `e-commerce-275d` |
| `climate` | **nothing** | `clean energy` → `clean-energy` |
| `ml` | **nothing** | `machine learning` → `machine-learning` |
| `ai` | *Agentic AI* (wrong) | `artificial intelligence` → `artificial-intelligence` |
| `SF` (location) | nothing | `san francisco` → `san-francisco-california` |

**Fix shipped:** `config/aliases.yaml > industry_aliases` maps each shorthand to the
exact `search` string Fundable indexes **and** the verified `permalink` to select;
`resolvers.py` rewrites the search term before the lookup and only picks a permalink
that actually appears in live results. `_norm` now strips spaces/hyphens so
`healthcare` == `health care` == `health-care`. Verified live: `healthcare startups in
Boston` → `health-care` (255 results) instead of dropping the filter.

**For Jacob:** this list is not exhaustive — it covers the common cases. A fuller fix is
to periodically pull Fundable's full industry list and build the alias/synonym table
from it (ties into the schema-drift item, #10). The mechanism is in place; it just needs
more entries.

## 1c. Fundable filters are INCLUDE-ONLY — no negation/exclusion  ⚠️ CONFIRMED (docs + live)
There is no way to express "not in X" / "excluding X" for locations or industries —
the API has no `excluded_locations`, `not_in`, or any negation operator; `locations`
and `industries` are include-only. So a query like *"fintech startups not in SF or NYC"*
literally cannot be filtered server-side.

**Danger:** naively, the planner extracted SF/NYC as positive filters → returned the
EXACT OPPOSITE (companies *in* SF/NYC). **Fix shipped:** the model now separates
`excluded_locations` / `excluded_industries`; the compiler keeps them OUT of the
positive filters and emits a clear warning ("Fundable's filters are include-only, so
it can't return results NOT in {X}…"). Confidence is capped so it's flagged.

**For Jacob:** if excluding regions matters to users, it must be done client-side
(fetch the include set, filter out the excluded permalinks in the app) — the API won't
do it. The planner already surfaces the excluded terms so the app can post-filter.

## 2. `search_query` + `industries` — API allows it, product forbids it
The API schema technically accepts both fields in the same block. The **product rule**
(§7) forbids it. So this is a *compiler-enforced* rule, not an API constraint — which
is exactly how it's implemented (`schema.py` rejects it). Confirm with Jacob that the
product rule is real and shouldn't just be passed through to the API.

## 3. Route ambiguity: "startups" vs "deals" is the main accuracy risk
"Find seed-stage AI startups in SF" → companies, but "…AI deals in SF" → deals. A
single verb ("raised") can flip intent. This is where route accuracy will leak below
the 90% target. Decisions needed:
- When do we silently default vs. return `needs_clarification`? (spec says clarify on
  low confidence — need a concrete threshold; `route_clarify_below: 0.6` is a
  placeholder.)
- The offline heuristic uses a fixed precedence (people > investor > deal > company);
  the LLM should do better, but this needs to be measured in Sprint 4/5.

## 4. `ipo_status` differs by endpoint — easy silent bug
- `companies` / `investors` / `deals`: `public`, `private`.
- **`people.company`: `public`, `private`, `acquired`, `delisted`** (extended set).
Emitting `acquired` to `/companies` is invalid. The schema map captures both sets
(`ipo_status_basic` vs `ipo_status_extended`); don't collapse them.

## 5. `/deals.company` has NO `search_query`
Unlike `/companies`, `/investors.company_investments`, and `/people.company`, the
company block on `/deals` is industry-only. If the planner wants semantic search on a
deals query, there's nowhere to put it — it must either drop it or switch route. Decide
the fallback behavior.

## 6. `search_query` overrides `sort_by`
On `/companies` and in `people.company` / `people.investor.deals`, setting
`search_query` makes the API ignore `sort_by` (relevance ordering). If a user asks for
"largest raises matching <semantic thing>", we can't honor both. Surface a warning; the
planner should not silently promise a sort that won't apply.

## 7. Named-entity resolution (companies/investors/people) is under-specified
Locations/industries resolve to permalinks; named firms/people resolve to **UUIDs** via
`/company/search`, `/investor/search`, `/people/search`. These are noisier (many
"Accel"s). Same clarify-vs-autopick problem as #1, but higher stakes (a wrong UUID is a
wrong entity, not just zero results). Needs its own disambiguation policy + UX.

## 8. Base URL / auth  ✅ RESOLVED
Confirmed live 2026-07-22. Base URL is **`https://www.tryfundable.ai/api/v1`** (not
`api.tryfundable.ai`, which does not resolve). Auth header `Authorization: Bearer vg_...`
works. Search endpoints report `credits_used: 0`. All four POST endpoints and both
search endpoints verified reachable and accepting our compiled bodies. `schema_map.yaml`
and `resolvers.py` now default to the correct host.

## 9. Person-block implication rules
`people`: any `company.*` field implies "has current employer"; any `investor.*` field
implies "is investor-flagged." These aren't validation errors but they change *who* is
returned. Worth encoding as planner warnings so results aren't silently narrowed.

## 10. Schema drift
Everything here is transcribed from the docs on 2026-07-22. Recommend generating the
schema map from the OpenAPI spec on build (or a CI check that diffs them), so an API
change surfaces as a test failure rather than silent wrong output. (Spec §17 leaves
this open; recommendation: check in the versioned file **and** add the CI diff.)
