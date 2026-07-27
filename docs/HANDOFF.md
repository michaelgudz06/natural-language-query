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

- **All 4 endpoints** — /deals, /companies, /investors, **/people** — with their
  documented filters (it picks the right one from the question, not always /companies)
- **People search** — "ex-Google founders in AI", "CTOs of healthcare startups in
  Boston", "Stanford founders" → extracts person roles, job titles, schools, and past
  employers (the exact same NL→filters experience, but for the people tab)
- **Aliases** — industry spellings Fundable needs (`healthcare`→`health-care`,
  `cybersecurity`→`cyber-security`) and 90 city nicknames (SF, Big Apple, Beantown, …)
- **Round ranges** — "Series C+" → Series C through M
- **Multiple locations** — "SF, NYC, and London"
- **Disambiguation** — ambiguous names (Springfield) become clickable choices
- **Exclusion** — "everywhere but America" (the API can't exclude, so it's done
  client-side: exact count-by-subtraction + result filtering)
- **Safety net** — a query that resolves to no filters is flagged, never silently
  returns "everything"

## 3. Results — full live evaluation (117 labeled cases)

| Metric | Result | Target |
|---|---|---|
| Route accuracy | **99%** | ≥90% ✅ |
| Request bodies API-valid | **100%** | ≥95% ✅ |
| JSON valid | **100%** | ≥95% ✅ |
| Resolver-backed fields (F1) | **99%** | ≥95% ✅ |
| Cost / 1,000 queries | **~$0.90** | — |
| Model latency p50 / p95 | 1.0s / 1.7s | — |
| End-to-end incl. parallel lookups p50 / p95 | 1.9s / 2.6s | — |
| **Acceptance criteria** | **11 / 11 pass** ✅ | — |

Per-endpoint route accuracy: /companies 100% · /deals 100% · /people 100% · /investors 94%.

Regenerate anytime: `python3 src/report.py` (writes the Report tab's data, live).

## 4. The demo — 4 tabs at localhost:8000

**Try** — type a question, watch it think. Try these to hit every feature:
1. `Series B fintech companies in New York that raised over $20M` — the basics
2. `Ex-Google founders in AI` — **people search** (roles + past employer + industry)
3. `Stanford founders who raised seed in fintech` — person school filter
4. `fintech companies, Series B+, everywhere but America` — round range + exclusion
5. `Most active VC firms investing in climate` — proves it's not company-only
6. `fintech companies in Springfield` — click a disambiguation option to lock it in

Each shows: **how it decided** (model reasoning + time + cost) · **filters it enabled**
· **what it looked up live** · **the query it built** · **live API result**.

**Model comparison** — every model tested (incl. Jacob's Chinese models: qwen, deepseek,
minimax) with the "single model wins" verdict. **Test set** — what the 117 cases cover
and why they're strong. **Report** — the 11/11 acceptance scorecard + per-endpoint accuracy.

## 5. Run it / test it

```bash
cd ~/fundable-query-planner
python3 tests/test_prod.py     # production-logic unit tests (offline)  → 18/18
python3 tests/test_smoke.py    # schema/pipeline unit tests (offline)   → 11/11
python3 serve.py               # the live 4-tab demo (needs keys)
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
| **Tests** | `tests/test_set.json` (117 cases, all 4 endpoints) + `test_prod.py`, `test_smoke.py` |
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
