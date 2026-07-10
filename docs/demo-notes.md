# OTB Weather Resolver — Full Demo Walkthrough

> Screenshare guide for a ~55-minute presentation.
> Flow: Architecture → show it working (Live + OTB) → Observability on live traces → prove determinism (Replay + Gold) → debugging.

---

## Pre-flight (run 10 minutes before screenshare)

```bash
cd /home/ramshreyas/Documents/Dev/UMA/OTB
source .venv/bin/activate

# 1. Confirm stack is running
docker ps --format "table {{.Names}}\t{{.Status}}"
# Expected: langfuse-web, langfuse-worker, litellm, postgres, clickhouse, redis, minio — all Up

# 2. Langfuse health
curl -s http://localhost:3000/api/public/health
# → {"status":"OK"}

# 3. LiteLLM health
curl -s -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models
# → model list

# 4. Confirm prompts are seeded
python scripts/seed_langfuse_prompts.py

# 5. Run 3 cases in live mode to generate fresh traces for the observability section
python resolve.py --input data/markets.json --fixtures data/fixtures --live \
  --case-id tokyo_high_2026_06_01_29c_or_higher --output output/live_preflight.json

python resolve.py --input data/markets.json --fixtures data/fixtures --live \
  --case-id seoul_low_2026_06_01_16c --output output/live_preflight.json

python resolve.py --input data/markets.json --fixtures data/fixtures --live \
  --case-id denver_high_2026_05_31_68_69f --output output/live_preflight.json

# 6. Seed the demo broken prompt (for Section 5a)
python demo/prompt_regression.py --force

# 7. Browser tabs:
#    - http://localhost:3000  (Langfuse — admin@otb.local / admin123)
#    - http://localhost:4000  (LiteLLM)
#    - Langfuse → Prompts tab
#    - Langfuse → Traces tab

# 8. Editor tabs:
#    - config/pipeline.yaml
#    - src/orchestration/steps.py
#    - src/orchestration/runner.py
```

---

## Section 1: Architecture Tour (5 min)

**Goal:** Show the system is a configurable DAG of stages, each automatically traced — not a giant prompt.

### 1.1 Open `config/pipeline.yaml`

| What to point at | Talk track |
|---|---|
| The `stages` list | "Five lines of YAML define the entire pipeline. Each stage has a module, a function, and an `on_error` policy. `unclear` means: if this stage throws, mark the case unresolvable." |
| Stage 2: `compose_spec` | "LLM + regex extraction of station, date, measurement from free-text ancillary data. The only stage that uses an LLM by default." |
| Stage 3: `retrieve` | "Fetches from Wunderground API, falls back to Playwright headless browser, or replays from fixture. `on_error: unclear` — if all paths fail, we don't guess the weather." |
| Stage 6: `decide` | "Deterministic mapping + conditional LLM reviewer. Reviewer only fires when confidence < 0.85." |
| The `llm` block | "All LLM config in one place: model, base URL (LiteLLM proxy), prompt names, thresholds. Switch models by changing one env var." |

### 1.2 How a YAML stage becomes a traced function

Open `src/orchestration/runner.py`, scroll to `_resolve_stage_fn()` (line ~402):

```python
def _resolve_stage_fn(stage_def: dict[str, Any]) -> Callable:
    module_path = stage_def["module"]       # e.g., "src.retrieval.spec"
    function_name = stage_def["function"]   # e.g., "compose_retrieval_spec"
    on_error = stage_def.get("on_error", "raise")

    mod = importlib.import_module(module_path)
    raw_fn = getattr(mod, function_name)

    from src.orchestration.steps import step

    @step(name=stage_name, stage_num=stage_num, on_error=on_error)
    def _wrapped(ctx, **kwargs):
        return _call_stage(raw_fn, ctx, **kwargs)

    return _wrapped
```

| What to point at | Talk track |
|---|---|
| `importlib.import_module` | "Reads `module` and `function` from YAML, resolves at startup. Add one line to YAML, it's picked up." |
| `@step(name=..., on_error=on_error)` | "The `on_error` from YAML flows into the decorator. `unclear` means: if this stage throws, mark unresolvable. No guessing." |

Now open `src/orchestration/steps.py`, scroll to `_trace_in_langfuse()` (line ~84):

```python
def _trace_in_langfuse(name, ctx, fn, deps):
    with client.start_as_current_observation(
        name=f"stage/{name}",
        as_type="span",
        input=_build_stage_input(name, ctx, deps),
    ):
        result = fn(ctx, **deps)
        client.update_current_span(output=_build_stage_output(name, result))
        return result
```

| What to point at | Talk track |
|---|---|
| `start_as_current_observation` | "Creates a span under the root trace. Every stage automatically appears in the waterfall." |
| `_build_stage_input / _build_stage_output` | "Each stage captures what it received and produced. Trace is fully self-documenting." |

> **Narrative:** "YAML defines what runs → `_resolve_stage_fn` wraps it with `@step` → `_trace_in_langfuse` creates the span. Define a new stage in YAML, it's automatically traced."

---

## Section 2: Live Mode + OTB (10 min)

**Goal:** Show it working against real data. This generates the traces we'll walk through next.

### 2.1 Live: Tokyo high (Wunderground API)

```bash
python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --live \
  --case-id tokyo_high_2026_06_01_29c_or_higher \
  --output output/live_demo.json
```

Narrate the log output as it runs:

```
Calling LLM for spec extraction...              ← LLM reads ancillary data
RetrievalSpec: station=RJTT aggregation=max    ← Extracted: Tokyo → Haneda Airport
Fetching Wunderground: RJTT 20260601            ← Primary API call
Observations: 48 → finality confirmed           ← 48 hourly readings, next-day data exists
Normalized: 31.0 C (completeness=1.00)          ← Daily high: 31°C
Market asked: ≥29°C? 31 ≥ 29 → Yes (p2)        ← Deterministic, confidence 0.95
```

> **Narrative:** "Three seconds end-to-end. Hits Wunderground, gets 48 hourly readings, extracts the daily high, compares against the market threshold. No giant prompt — each stage is a discrete step with a traceable output."

### 2.2 Live: Denver range (station mapping)

```bash
python resolve.py \
  --input data/markets.json --fixtures data/fixtures --live \
  --case-id denver_high_2026_05_31_68_69f --output output/live_demo.json
```

Point out the station mapping: title says "Denver" but the LLM extracts `KBKF` (Buckley SFB, Aurora CO) from the ancillary data URL. The station registry attaches the note: *"Market title says 'Denver' but resolution source is Buckley SFB (KBKF) in Aurora, CO."* — visible in the Langfuse `stage/compose_spec` span output under `cross_validation`.

> **Narrative:** "This is the Denver/Buckley problem the brief warns about. We don't infer the station from the city name — the URL in ancillary data is authoritative. The station registry flags the mismatch for operator visibility."

### 2.3 Live OTB: production proposed markets

```bash
python resolve_otb.py --max-markets 3
```

| Beat | Talk track |
|---|---|
| OTB API fetch | "Same resolver — different entry point. Polls the OTB Oracle API for live proposed Weather markets." |
| Transform to MarketCase | "OTB API items are transformed into the same `MarketCase` objects `resolve.py` uses. The pipeline doesn't know the difference." |
| Paper-propose | "Returns p1/p2/p3/p4/unclear recommendations. No settlement — paper-propose only. The team can compare our resolution against real-time markets." |
| Fixture auto-save | "Live mode writes raw responses to `data/fixtures/`. Next time, replay mode uses that fixture. The system bootstraps its own determinism." |

---

## Section 3: Observability (10 min)

**Goal:** Walk through the traces generated by the live runs. Show Langfuse prompt registry, trace waterfall, and LiteLLM.

### 3.1 Langfuse Prompt Registry (3 min)

Open Langfuse → **Prompts**. Show `weather-spec-extraction`:

| Element | Talk track |
|---|---|
| Labels: `production`, `latest`, `demo-broken` | "The resolver fetches whatever has `production`. Create a new version, test with `staging`, promote. Rollback is a label change — zero code deploy." |
| Template variables: `{{title}}`, `{{ancillary_data}}` | "Filled at runtime. The template stays in Langfuse." |
| Version dropdown | "Full history. Every edit tracked." |

Show `weather-reviewer`:

| Element | Talk track |
|---|---|
| Constraint: `Answer ONLY with {"agree": true} or {"agree": false}` | "Can only escalate to `unclear`. Can't substitute its own p1/p2. Deliberate guardrail for conservatism." |

### 3.2 Trace Waterfall (5 min)

Open Langfuse → **Traces**. Click the most recent live trace.

```
resolve/tokyo_high_2026_06_01_29c_or_higher
├── stage/validate
├── stage/compose_spec
│   └── spec-extraction          ← GENERATION (auto-captured LLM call)
├── stage/retrieve                ← Wunderground API call recorded
├── stage/normalize               ← completeness, quality flags
├── stage/reconcile               ← threshold comparison
└── stage/decide                  ← p2, confidence 0.95, deterministic
```

| Beat | What to show |
|---|---|
| **Root span output** | `recommendation: "p2"`, `confidence: 0.95`, `observed_value: 31.0`. "Answer at a glance." |
| **stage/retrieve span** | `observation_count: 48`, `finality: "confirmed"`, source_trace with exact URLs and latencies. "Every source query recorded." |
| **spec-extraction GENERATION** | Auto-captured: model, input, output JSON, tokens, latency. Linked to `weather-spec-extraction` prompt. Click link → jumps to prompt page. "No manual instrumentation. The observability loop closes: trace → prompt version → fix." |
| **stage/normalize span** | Completeness, quality flags, unit conversion. "Every normalization decision recorded." |

### 3.3 LiteLLM Proxy (2 min)

Open `http://localhost:4000` → Dashboard. Then `cat litellm_config.yaml`:

> **Narrative:** "Provider-agnostic routing. DeepSeek today — add GPT-4 or Claude with one line. Every proxied request logged independently of Langfuse."

---

## Section 4: Replay + vs Gold (5 min)

**Goal:** Prove determinism. Same answers, no network, matches Polymarket resolutions.

### 4.1 Run all 5 in replay mode

```bash
python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --output output/results.json \
  --gold gold_visible/answers.json
```

### 4.2 Show the results

```
============================================================
Case ID                                   Expected  Rec     Conf
------------------------------------------------------------
tokyo_low_2026_06_01_20c                  p2        p2      0.70  ✓
tokyo_high_2026_06_01_29c_or_higher       p2        p2      0.95  ✓
busan_high_2026_06_01_22c_or_below        p1        p1      0.95  ✓
seoul_low_2026_06_01_16c                  p2        p2      0.95  ✓
denver_high_2026_05_31_68_69f             p1        p1      0.95  ✓
------------------------------------------------------------
Match: 5/5 (100%)
```

> **Narrative:** "Five for five. Determined the same way each time. The Tokyo low case had partial fixture data — confidence dropped below 0.85, reviewer was invoked, confirmed at 0.70. Same trace structure as live mode — replay just swaps the network call for a fixture load."

### 4.3 Show evidence quality

```bash
python evaluate.py --predictions output/results.json --gold gold_visible/answers.json
```

Point at the completeness, decision path, and source columns. "Every answer comes with its evidence chain."

---

## Section 5: Debugging with Observability (15 min)

**Goal:** Now that they've seen it work, show how observability catches real problems.

### 5a. Prompt Regression (5 min)

**Story:** Someone promotes a bad prompt. How do we detect and fix it?

```bash
# Show the broken prompt exists
python demo/prompt_regression.py

# Promote → run → rollback (the full cycle)
python demo/prompt_regression.py --promote --case-id tokyo_low_2026_06_01_20c --yes
```

In Langfuse, refresh Traces → find the broken run:

| What to point at | Talk track |
|---|---|
| `stage/compose_spec` output | `aggregation: "max"` — wrong! Market asked for "lowest temperature." |
| `spec-extraction` GENERATION | Linked to the broken prompt version. Full prompt content visible. |
| Root span | Wrong recommendation. |

> **Narrative:** "The trace links to the exact prompt version that caused it. Fix: roll back the label. No code deploy, no restart. The trace told us the what, the why, and the fix — in one view."

### 5b. Unclear Case (5 min)

**Story:** A market returned `unclear`. Why?

```bash
python resolve.py \
  --input data/demo_cases.json \
  --fixtures data/fixtures/demo \
  --output output/demo_unclear_results.json
```

The fixture has `observations: []`, `extracted_value: null`. Pipeline output:

```
stage_04_normalize: FAILED — no_observations: No observations in target window
Pipeline short-circuited at stage 'normalize': unclear
```

In Langfuse trace:

| What to point at | Talk track |
|---|---|
| `stage/retrieve` output | `observation_count: 0`. "Data was fetched but empty." |
| `stage/normalize` span | Hard gate: `no_observations`. Terminal reason: `unclear`. |
| Root span output | `recommendation: "unclear"`. "At a glance: no data → unclear. No guess was made." |

Compare side-by-side with a healthy trace from Section 2 (48 observations, confidence 0.95).

### 5c. Service Failure (5 min)

**Story:** LiteLLM goes down. Does the system fall apart?

```bash
docker compose stop litellm

python resolve.py --input data/markets.json --fixtures data/fixtures \
  --case-id tokyo_high_2026_06_01_29c_or_higher --output output/demo_llm_down.json
```

Terminal output:
```
Calling LLM for spec extraction (attempt 1/3)...  Retrying...
LLM extraction failed after 3 attempts — falling back to regex
Regex extraction succeeded: station=RJTT aggregation=max method=regex
```

In Langfuse trace: `spec-extraction` GENERATION shows error status, but `stage/compose_spec` completed with `extraction_method: "regex"`.

```bash
docker compose start litellm
```

> **Narrative:** "LLM unavailable. Three retries with backoff. Regex fallback kicks in — successfully parses station, date, measurement. Pipeline continues. Answer is correct. The trace documents the entire fallback: what failed, what succeeded, what path was taken."

---

## Architecture Recap

```
resolve.py / resolve_otb.py
  │
  ├─ PipelineRunner.run()                          ← config/pipeline.yaml
  │   └─ _run_case_with_trace()                    ← Langfuse root trace per case
  │       │
  │       ├─ stage/validate                        [span]
  │       ├─ stage/compose_spec                    [span via @step]
  │       │   └─ spec-extraction                   [GENERATION — langfuse.openai]
  │       │       ├─ Prompt: weather-spec-extraction (Langfuse)
  │       │       └─ LiteLLM proxy → LLM | regex fallback
  │       ├─ stage/retrieve                        [span via @step]
  │       │   ├─ Replay: fixture | Live: API → Playwright → unclear
  │       │   └─ Auto-save fixture
  │       ├─ stage/normalize                       [span via @step]
  │       ├─ stage/reconcile                       [span via @step]
  │       └─ stage/decide                          [span via @step]
  │           └─ [if conf < 0.85] reviewer-check   [GENERATION]
  │
  └─ flush_langfuse() → traces in UI
```

---

## Key Files

| File | Role |
|---|---|
| `config/pipeline.yaml` | Pipeline stage definitions, LLM config |
| `src/orchestration/runner.py` | PipelineRunner, YAML→stage resolution, trace per case |
| `src/orchestration/steps.py` | `@step` decorator, span creation |
| `src/observability/llm.py` | LLMClient with `langfuse.openai.OpenAI` |
| `src/retrieval/dispatch.py` | Retrieval dispatch (live fallback tree, replay) |
| `src/decision/resolver.py` | Deterministic mapping + conditional LLM reviewer |
| `scripts/seed_langfuse_prompts.py` | Seeds prompts into Langfuse |
| `demo/prompt_regression.py` | Prompt regression demo (promote → run → rollback) |
| `data/demo_cases.json` + `data/fixtures/demo/` | Demo fixtures for unclear/failure scenarios |

---

## Troubleshooting

### Traces not appearing in Langfuse

```bash
python -c "from src.observability.tracing import get_langfuse_client; print(get_langfuse_client())"
grep LANGFUSE .env
```

### LiteLLM can't reach model

```bash
curl -s -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models
docker compose restart litellm
```

### Demo unclear case resolves instead of gating

The fixture at `data/fixtures/demo/RJTT_20260601_temperature_min.json` must have `observations: []` and `extracted_value.value: null`. Verify:

```bash
python -c "
import json
with open('data/fixtures/demo/RJTT_20260601_temperature_min.json') as f:
    d = json.load(f)
print('observations:', len(d.get('observations', [])))
print('extracted_value:', d.get('extracted_value', {}))
"
```
