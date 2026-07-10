# OTB Weather Resolver — Full Demo Walkthrough

> Screenshare guide for a ~55-minute presentation.
> Flow: Architecture → Live runs with interleaved traces → Replay + Gold → Debugging.
> Observability is woven into each live call — not a separate section.

---

## Pre-flight (run 10 minutes before screenshare)

```bash
cd /home/ramshreyas/Documents/Dev/UMA/OTB
source .venv/bin/activate

# 1. Confirm stack is running
docker ps --format "table {{.Names}}\t{{.Status}}"

# 2. Langfuse + LiteLLM health
curl -s http://localhost:3000/api/public/health
curl -s -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models

# 3. Confirm prompts are seeded
python scripts/seed_langfuse_prompts.py

# 4. Run 2 live cases to generate traces (Tokyo high, Denver)
python resolve.py --input data/markets.json --fixtures data/fixtures --live \
  --case-id tokyo_high_2026_06_01_29c_or_higher --output output/live_preflight.json

python resolve.py --input data/markets.json --fixtures data/fixtures --live \
  --case-id denver_high_2026_05_31_68_69f --output output/live_preflight.json

# 5. Seed demo broken prompt (for Section 7a — just creates the version with demo-broken label)
python demo/prompt_regression.py --force
# Note: the actual promote/rollback in Section 7a is done manually in the Langfuse UI

# 6. Browser tabs:
#    - http://localhost:3000  (Langfuse — admin@otb.local / admin123)
#    - http://localhost:4000  (LiteLLM)
#    - Langfuse → Prompts tab
#    - Langfuse → Traces tab (two tabs: one for Tokyo, one for Denver)

# 7. Editor tabs:
#    - config/pipeline.yaml
#    - src/orchestration/runner.py  (_resolve_stage_fn)
#    - src/orchestration/steps.py   (_trace_in_langfuse)
```

---

## Section 1: Architecture (5 min)

**Goal:** Show it's a configurable DAG of stages, each auto-traced. Not a giant prompt.

### 1.1 `config/pipeline.yaml`

| What to point at | Talk track |
|---|---|
| The `stages` list | "Five lines of YAML define the pipeline. Each stage: module, function, `on_error` policy. `unclear` means if this stage throws, mark unresolvable rather than guessing." |
| Stage 2: `compose_spec` | "LLM + regex extraction of station, date, measurement from free-text ancillary data." |
| Stage 3: `retrieve` | "Wunderground API → Playwright fallback → replay from fixture." |
| Stage 6: `decide` | "Deterministic mapping + conditional LLM reviewer. Reviewer fires only when confidence < 0.85." |
| The `llm` block | "All LLM config: model, LiteLLM proxy URL, prompt names, thresholds. Switch model = change one env var." |

### 1.2 YAML → traced function

Open `runner.py` → `_resolve_stage_fn()`:

```python
mod = importlib.import_module(stage_def["module"])   # "src.retrieval.spec"
raw_fn = getattr(mod, stage_def["function"])          # "compose_retrieval_spec"

@step(name=stage_name, on_error=stage_def["on_error"])
def _wrapped(ctx, **kwargs):
    return _call_stage(raw_fn, ctx, **kwargs)
```

Open `steps.py` → `_trace_in_langfuse()`:

```python
with client.start_as_current_observation(name=f"stage/{name}", as_type="span",
        input=_build_stage_input(name, ctx, deps)):
    result = fn(ctx, **deps)
    client.update_current_span(output=_build_stage_output(name, result))
```

> **Narrative:** "YAML defines what runs. `_resolve_stage_fn` imports it, wraps it with `@step`. `_trace_in_langfuse` creates the span. Define a new stage in YAML → it's automatically traced. ~30 lines of decorator code."

---

## Section 2: Tokyo High — Live + Trace (5 min)

### 2.1 Run it

```bash
python resolve.py \
  --input data/markets.json --fixtures data/fixtures --live \
  --case-id tokyo_high_2026_06_01_29c_or_higher \
  --output output/live_demo.json
```

Narrate the terminal output:

```
Calling LLM for spec extraction...              ← LLM reads ancillary data
RetrievalSpec: station=RJTT aggregation=max    ← Tokyo Haneda, daily high
Fetching Wunderground: RJTT 20260601            ← Primary API call
Observations: 48 → finality confirmed           ← 48 hourly readings, next-day data exists
Normalized: 31.0 C (completeness=1.00)          ← Daily high: 31°C
Market asked: ≥29°C? 31 ≥ 29 → Yes (p2)        ← Deterministic, confidence 0.95
```

> **Narrative:** "Three seconds. Hits Wunderground, 48 hourly readings, extracts high, compares. No giant prompt — each stage is a discrete step."

### 2.2 Walk its trace

Switch to Langfuse → Traces → click the Tokyo trace:

```
resolve/tokyo_high_2026_06_01_29c_or_higher
├── stage/validate
├── stage/compose_spec
│   └── spec-extraction          ← GENERATION (auto-captured)
├── stage/retrieve
├── stage/normalize
├── stage/reconcile
└── stage/decide
```

| Beat | Show | Talk track |
|---|---|---|
| Root span output | `recommendation: "p2"`, `confidence: 0.95`, `observed_value: 31.0` | "Answer at a glance." |
| `stage/retrieve` | Input: `station_code: "RJTT"`. Output: `observation_count: 48`, `finality: "confirmed"`, source_trace with exact URLs and latencies | "Every source query recorded." |
| `spec-extraction` GENERATION | Model, input prompt, output JSON, tokens, latency. Click linked prompt → jumps to prompt page | "Auto-captured. Zero manual instrumentation. Click the prompt link — this is the observability loop closing." |
| `stage/normalize` | `completeness: 1.00`, `value: 31.0` | "Every normalization decision documented." |

---

## Section 3: Denver — Live + Trace (4 min)

### 3.1 Run it

```bash
python resolve.py \
  --input data/markets.json --fixtures data/fixtures --live \
  --case-id denver_high_2026_05_31_68_69f \
  --output output/live_demo.json
```

### 3.2 Station mapping in the trace

Switch to Langfuse → Traces → click the Denver trace → `stage/compose_spec`:

Point at `cross_validation` in the span output:
```
station_city_awareness: false
station_city_awareness_detail: "Market title says 'Denver' but resolution source
  is Buckley SFB (KBKF) in Aurora, CO."
```

> **Narrative:** "Title says Denver. Ancillary data URL says KBKF in Aurora. The LLM extracts from the URL — never the city name. The station registry flags the mismatch here. Compare with the Tokyo trace: no mismatch, no note. Side by side, the difference is obvious."

---

## Section 4: Live OTB + Trace (3 min)

### 4.1 Run it

```bash
python resolve_otb.py --max-markets 3
```

| Beat | Talk track |
|---|---|
| OTB API fetch | "Same resolver — different entry point. Polls OTB Oracle API for live proposed Weather markets." |
| Transform + run | "OTB items → MarketCase objects → same `runner.run_cases()`. Pipeline doesn't know the difference." |
| Paper-propose | "p1/p2/p3/p4/unclear. No settlement. Compare our resolution against real-time markets." |
| Fixture auto-save | "Live mode writes responses to `data/fixtures/`. Replay bootstraps its own determinism." |

### 4.2 Trace comparison

Open an OTB trace in Langfuse. Same waterfall structure — `resolve/{case_id}` → same 7 stage spans. "Different entry point, same pipeline, same trace."

---

## Section 5: Prompt Registry (2 min)

Switch to Langfuse → **Prompts**. Quick tour:

| Prompt | What to show | Talk track |
|---|---|---|
| `weather-spec-extraction` | Labels, version dropdown, `{{title}}` `{{ancillary_data}}` vars | "Resolver fetches whatever has `production` label. Promote a new version: label change. Rollback: label change. Zero code deploy." |
| `weather-reviewer` | Constraint: `Answer ONLY with {"agree": true} or {"agree": false}` | "Can only escalate to unclear. Can't substitute p1/p2. Deliberate conservatism guardrail." |
| `demo-broken` label | Point at it in the dropdown, don't click yet | "We'll come back to this in the debugging section." |

---

## Section 6: Replay + vs Gold (4 min)

**Goal:** Prove determinism. Same answers, no network.

```bash
python resolve.py \
  --input data/markets.json --fixtures data/fixtures \
  --output output/results.json --gold gold_visible/answers.json
```

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

> **Narrative:** "All five match Polymarket resolutions. Tokyo low had partial fixture data — confidence dropped below 0.85, reviewer invoked, confirmed at 0.70. Same answer as live mode, no network — the fixture is the replay anchor. Run `evaluate.py` for evidence-quality breakdown per case."

---

## Section 7: Debugging with Observability (15 min)

**Goal:** They've seen it work. Now show how observability catches real problems.

### 7a. Prompt Regression (5 min)

**Story:** Someone accidentally promotes a bad prompt to production.

#### Step 1: Show the broken prompt
Open Langfuse → **Prompts** → `weather-spec-extraction`. Version dropdown → click the version with `demo-broken` label. Show its content:

> `"- aggregation: ALWAYS use \"max\" regardless of what the question asks."`

Now flip to the `production` version:

> `"- aggregation: \"min\" for lowest/minimum, \"max\" for highest/maximum..."`

#### Step 2: Promote it (the mistake)
Edit the `demo-broken` version's labels — add `production`. Save.

> **Narrative:** "Someone accidentally added 'production' to the wrong version. Bad prompt is now live."

#### Step 3: Run and see it break

```bash
python resolve.py --input data/markets.json --fixtures data/fixtures \
  --case-id tokyo_low_2026_06_01_20c --output output/demo_broken.json
```

Terminal shows `aggregation=max` — wrong. Market asked for "lowest temperature."

#### Step 4: Trace reveals the cause
Refresh Langfuse → Traces → click the broken run:

| Trace element | What it shows |
|---|---|
| `stage/compose_spec` output | `aggregation: "max"` — wrong |
| `spec-extraction` GENERATION | Linked to the broken prompt version. Full prompt content visible |
| Root span output | Wrong recommendation |

#### Step 5: Rollback
Back in Prompts → find the correct version → add `production` label. Save.

```bash
python resolve.py --input data/markets.json --fixtures data/fixtures \
  --case-id tokyo_low_2026_06_01_20c --output output/demo_fixed.json
```

Correct answer again.

> **Narrative:** "Trace linked to the exact prompt version. Fix: change the label back in the UI. No code deploy, no restart. Trace showed what, why, and the fix — in one view."

### 7b. Unclear Case (5 min)

**Story:** A market returned `unclear`. Why?

```bash
python resolve.py \
  --input data/demo_cases.json --fixtures data/fixtures/demo \
  --output output/demo_unclear_results.json
```

Fixture has `observations: []`, `extracted_value: null`. Terminal:

```
stage_04_normalize: FAILED — no_observations
Pipeline short-circuited at stage 'normalize': unclear
```

In Langfuse trace for this run:

| Span | Shows |
|---|---|
| `stage/retrieve` output | `observation_count: 0` |
| `stage/normalize` | Hard gate: `no_observations`. Terminal: `unclear` |
| Root span | `recommendation: "unclear"` |

Compare side-by-side with Tokyo trace (48 observations, confidence 0.95). "Difference is immediate. Operator doesn't guess — trace tells them."

### 7c. Service Failure (5 min)

**Story:** LiteLLM goes down. Does it fall apart?

```bash
docker compose stop litellm

python resolve.py --input data/markets.json --fixtures data/fixtures \
  --case-id tokyo_high_2026_06_01_29c_or_higher --output output/demo_llm_down.json
```

Terminal:
```
Calling LLM for spec extraction (attempt 1/3)...  Retrying...
LLM extraction failed after 3 attempts — falling back to regex
Regex extraction succeeded: station=RJTT aggregation=max method=regex
```

In Langfuse: `spec-extraction` GENERATION shows error, but `stage/compose_spec` completed with `extraction_method: "regex"`.

```bash
docker compose start litellm
```

> **Narrative:** "LLM unavailable. Three retries. Regex fallback parses station, date, measurement. Pipeline continues. Answer unchanged. Trace documents every step of the fallback chain."

---

## Architecture Recap

```
resolve.py / resolve_otb.py
  │
  ├─ PipelineRunner.run()                          ← config/pipeline.yaml
  │   └─ _run_case_with_trace()                    ← Langfuse root trace per case
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
| `scripts/seed_langfuse_prompts.py` | Seeds prompts into Langfuse |
| `demo/prompt_regression.py` | Prompt regression demo |

---

## Troubleshooting

```bash
# Langfuse
curl -s http://localhost:3000/api/public/health
grep LANGFUSE .env

# LiteLLM
curl -s -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models
docker compose restart litellm

# Stack
docker compose down && docker compose up -d
```
