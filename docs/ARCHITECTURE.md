# Architecture & the decisions behind it

Read [HANDOFF.md](HANDOFF.md) first for the what/why. This is the how.

## The two-phase pipeline

```
   sentence
      │
      ▼ PHASE 1 — planner (one LLM call)         src/planner.py + config/planner.yaml
      │   route + filters (as display names) + which lookups are needed + reasoning
      │   NEVER emits a permalink/ID
      ▼
      ├─ deterministic scalar extraction          src/extractors.py
      │   money ($5M), dates (since 2023), round ranges (Series C+ → C…M), sizes
      ├─ canonical path mapping                    src/pathmap.py
      ▼ PHASE 2a — resolve (in PARALLEL)           src/resolvers.py (+ aliases)
      │   location/industry/entity lookups run concurrently (ThreadPoolExecutor);
      │   aliases fix Fundable's literal search (healthcare→health-care, SF→…);
      │   ambiguous names → clarification; excluded terms → handled client-side
      ▼ PHASE 2b — compile + validate              src/compiler.py + src/schema.py
      │   assemble body, enforce rules, validate against schema/schema_map.yaml
      ▼
   valid API body  +  what-it-looked-up  +  filters enabled  +  warnings
```

**Why the split:** LLMs interpret fuzzy language well but occasionally invent values;
deterministic code is rigid but exact. So the LLM's output is *advisory* and the
compiler + schema gate are *authoritative* — an unsupported field or bad enum is
rejected/repaired before anything is sent. That's why bodies are 100% API-valid
regardless of the model.

**Parallelization (measured):** 5 lookups take **1,482 ms in parallel vs 5,528 ms
sequential (3.7× faster)**. Resolver latency is reported separately from model latency.

## Module map

| File | Job |
|------|-----|
| `config/planner.yaml` | prompts, production model, thresholds |
| `config/round_types.yaml`, `config/aliases.yaml` | round mapping; industry + 90 city aliases |
| `schema/schema_map.yaml` | the full Fundable API contract (all 4 endpoints) |
| `src/planner.py` | Phase 1 — the LLM call + flat-output → plan adapter |
| `src/extractors.py`, `src/pathmap.py` | deterministic money/date/round parsing; canonical paths |
| `src/resolvers.py` | Phase 2a — parallel lookups, aliases, disambiguation |
| `src/compiler.py`, `src/schema.py` | Phase 2b — assemble + validate |
| `src/exclude.py` | client-side "not in X" (count-by-subtraction) |
| `src/pipeline.py` | orchestrates it all; `production_pipeline()` is the entry point |
| `src/report.py` etc. | evaluation harness + bakeoffs |

## The model & architecture decision (from the bakeoff)

**Models compared** (109 cases, planning quality, OpenRouter, live):

| model | route acc | intent | $/1k | latency p50 |
|---|---|---|---|---|
| **gemini-3.5-flash-lite** ⭐ | **96%** | 95% | $0.60 | **999ms** |
| gemini-2.5-flash | 95% | 94% | $0.57 | 1237ms |
| gpt-4o-mini | 93% | 95% | $0.18 | 1802ms |
| gpt-5.1 | 91% | 95% | $2.18 | 1810ms |
| gemma-4-31b | 93% | 95% | $0.13 | 3422ms |
| gemini-2.5-flash-lite | 86% | 91% | $0.13 | 903ms |

*Surprise: the priciest model (gpt-5.1) wasn't the most accurate.*

**Architectures compared** (single vs router vs orchestrator/executor, 30 cases):
every reasonable architecture landed at ~92–97% route accuracy. The **confidence
router never fired** (the cheap model was always confident enough, so it degenerated
to single-model at higher cost), and the **orchestrator/executor split added latency
without accuracy**.

**Decision: single model, `gemini-3.5-flash-lite`, no router** — tied for the best
accuracy at the fastest latency, and the simplest thing that works (per Jacob). Swap
the model in `config/planner.yaml > production.model`; re-run `src/arch_bakeoff.py` /
`src/model_compare.py` to re-measure.

## Confidence (calibration finding)

The model's self-reported confidence is bimodal: **"exactly 100%" is a perfect
predictor (58/58 right); the misses hide in the 90–99% band (79% right).** So a
low-confidence threshold doesn't catch errors — they sit just under 100%. We also
tested "reflect-and-retry" (re-ask the model on shaky cases): **no accuracy gain, +46%
cost, +40% latency** — so it's not used. If clarification is wanted, the right move is
a non-blocking "did you mean…?" toggle plus the existing clickable disambiguation, not
a retry loop.
