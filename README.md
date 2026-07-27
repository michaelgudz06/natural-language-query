# Fundable NL Query Planner

Type a plain-English question ("fintech companies in Boston that raised Series C+")
→ it picks the right Fundable endpoint, looks up the real IDs, builds a valid API
query, and runs it live. Single model (`gemini-3.5-flash-lite`), no router.

**→ Start with [`docs/HANDOFF.md`](docs/HANDOFF.md).**

## Quickstart

```bash
cd ~/fundable-query-planner
python3 tests/test_prod.py     # production-logic unit tests (offline, no keys) → 18/18
python3 tests/test_smoke.py    # schema/pipeline unit tests (offline)           → 11/11
python3 serve.py               # live 4-tab demo at localhost:8000  (needs keys)
python3 src/report.py          # full acceptance scorecard, live    (needs keys)
```

Keys go in a `.env` file:
```
OPENROUTER_API_KEY=...
FUNDABLE_API_KEY=...
FUNDABLE_BASE_URL=https://www.tryfundable.ai/api/v1
```

## Docs (that's all three)

- **[HANDOFF.md](docs/HANDOFF.md)** — what it is, the flow, the demo, results, how to run/send.
- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — how it's built + the model/eval decisions.
- **[GAPS.md](docs/GAPS.md)** — real Fundable API limitations & open decisions.

## Layout

```
config/     prompts, model choice, round mapping, industry + city aliases
schema/     schema_map.yaml — the full Fundable API contract (all 4 endpoints)
src/        the planner (planner, pipeline, resolvers, compiler, schema, extractors,
            pathmap, exclude, filters_view) + eval tooling (report, model_compare,
            arch_bakeoff, calibrate, harness)
tests/      test_set.json (117 labeled cases, all 4 endpoints incl. people) + test_prod.py + test_smoke.py
serve.py    the live 4-tab demo (Try · Model comparison · Test set · Report)
```

Entry point in code: `pipeline.production_pipeline()`.
