# Fundable NL Query Planner — Handoff

**Type a plain-English question → it picks the right Fundable endpoint, looks up the
real IDs, builds a valid API query, and runs it live.** Single model, no router.

- **How it's built + why these choices:** [ARCHITECTURE.md](ARCHITECTURE.md)
- **Real Fundable API limitations & open decisions:** [GAPS.md](GAPS.md)
- **Live demo:** `python3 serve.py` → http://localhost:8000

---

## 1. How the flow works (two phases)

```
your question
   │
   ▼  PHASE 1 — the model (gemini-3.5-flash-lite)
   ├─ picks the endpoint: /deals · /companies · /investors · /people
   ├─ pulls out the filters (industry, location, round, size, dates, …)
   └─ flags what needs a lookup — but NEVER guesses an ID
   │
   ▼  PHASE 2 — deterministic code (no AI, fully predictable)
   ├─ runs the lookups IN PARALLEL (location + industry + investor at once)
   ├─ maps rounds/dates/money, applies aliases (healthcare→health-care, SF→…)
   ├─ builds the final query and VALIDATES it against the API schema
   └─ hits the live Fundable API
```

The split is deliberate: **the model interprets, the code does anything that must be
exact.** That's why request bodies are 100% API-valid regardless of the model.

## 2. What it handles

- **All 4 endpoints** with their documented filters
- **Aliases** — industry spellings Fundable needs (`healthcare`→`health-care`,
  `cybersecurity`→`cyber-security`) and 90 city nicknames (SF, Big Apple, Beantown,
  ATX, NOLA, Bay Area, …)
- **Round ranges** — "Series C+" → Series C through M
- **Multiple locations** — "SF, NYC, and London"
- **Disambiguation** — ambiguous names (Springfield) become clickable choices
- **Exclusion** — "everywhere but America" (the API can't exclude, so it's done
  client-side: exact count-by-subtraction + result filtering)
- **Safety net** — a query that resolves to no filters is flagged, never silently
  returns "everything"

## 3. Results — full 109-case live evaluation

| Metric | Result | Target |
|---|---|---|
| Route accuracy | **93%** | ≥90% ✅ |
| Request bodies API-valid | **100%** | ≥95% ✅ |
| JSON valid | **100%** | ≥95% ✅ |
| Cost / 1,000 queries | **~$0.69** | — |
| Model latency p50 / p95 | 1.1s / 1.6s | — |
| End-to-end incl. parallel lookups p50 / p95 | 2.1s / 2.7s | — |
| **Acceptance criteria** | **10 / 11 pass** | — |

The one below bar — resolver-backed fields 92% vs 95% — measures how closely the
model's list of "things to look up" matches our hand-written answer key. It's label
agreement, not broken output, and rises as the test set is refined.

Regenerate anytime: `python3 src/report.py` (writes the Report tab's data).

## 4. The demo — 3 tabs at localhost:8000

**Try** — type a question, watch it think. Try these to hit every feature:
1. `Series B fintech companies in New York that raised over $20M` — the basics
2. `AI startups in Beantown and the Big Apple` — nickname aliases resolving
3. `fintech companies, Series B+, everywhere but America` — round range + exclusion
4. `fintech companies in Springfield` — click a disambiguation option to lock it in

Each shows: **how it decided** (model reasoning + time + cost) · **filters it enabled**
· **what it looked up live** · **the query it built** · **live API result**.

**Model comparison** — the models & architectures tested, with the "single model wins"
verdict. **Report** — the acceptance scorecard + full metrics.

## 5. Run it / test it

```bash
cd ~/fundable-query-planner
python3 tests/test_prod.py     # production-logic unit tests (offline)  → 13/13
python3 tests/test_smoke.py    # schema/pipeline unit tests (offline)   → 11/11
python3 serve.py               # the live 3-tab demo (needs keys)
python3 src/report.py          # full acceptance scorecard, live (needs keys)
```
The unit tests run offline with no keys (good for CI). The demo + report need a
`.env` with `OPENROUTER_API_KEY` and `FUNDABLE_API_KEY`.

## 6. What to send + how to package

Send the **whole `~/fundable-query-planner` folder** — it's self-contained.

```bash
cd ~
rm -f fundable-query-planner/.env          # ⚠ strip the API keys FIRST
zip -r fundable-query-planner.zip fundable-query-planner \
    -x "*/__pycache__/*" "*/.git/*" "*.pyc" "*/planner.log.jsonl"
```

When Jacob's ready to run it, he creates his own `.env`:
```
OPENROUTER_API_KEY=...     # his OpenRouter key
FUNDABLE_API_KEY=...       # Fundable's key
FUNDABLE_BASE_URL=https://www.tryfundable.ai/api/v1
```

| Area | Files |
|---|---|
| **Docs** | this HANDOFF · ARCHITECTURE · GAPS (that's it) |
| **The planner** | `src/` — planner, pipeline, resolvers, compiler, schema, extractors, pathmap, exclude, filters_view |
| **Eval tooling** | `src/` — report, model_compare, arch_bakeoff, calibrate, harness |
| **Config** | `config/` (prompts, aliases, models) + `schema/schema_map.yaml` |
| **Tests** | `tests/test_set.json` (109 cases) + `test_prod.py`, `test_smoke.py` |
| **Demo** | `serve.py`, `Start Tester.command` |

## 7. Glossary (plain English)

- **Route** — which of the 4 Fundable endpoints a question maps to.
- **Resolver / lookup** — a call to Fundable's search to turn a name into an exact ID
  ("Boston" → `boston-massachusetts`). "Resolver-backed fields" are the ones that need
  this (locations, industries, named companies/investors).
- **Permalink** — Fundable's exact ID for a place/industry. The planner never guesses
  one; it always looks it up.
- **Single model, no router** — one AI call does the interpreting; a "router" (send hard
  cases to a pricier model) was tested and dropped because it didn't help. See
  ARCHITECTURE.
- **Acceptance criteria** — the spec's pass/fail bar (route ≥90%, bodies ≥95% valid, …),
  checked live by `src/report.py`.
