# OTB Weather Resolver — Full Demo Walkthrough

> Screenshare guide for a ~55-minute presentation. Follow section by section.
> Each section has commands, what to show, and a talk track.

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
# → {"status":"OK","version":"3.205.0"}

# 3. LiteLLM health
curl -s -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models
# → {"data":[{"id":"deepseek-chat",...}]} or {"data":[{"id":"gemini-2.5-flash",...}]}

# 4. Confirm prompts are seeded
python scripts/seed_langfuse_prompts.py
# → ✓ Created prompt: weather-spec-extraction (label: production)
# → ✓ Created prompt: weather-reviewer (label: production)

# 5. Run all 5 cases in replay mode (generates traces for observability section)
python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --output output/results.json \
  --gold gold_visible/answers.json

# 6. Seed the demo broken prompt (for Section 5a — safe, uses demo-broken label)
python demo/prompt_regression.py --force

# 7. Open these in browser tabs:
#    - http://localhost:3000  (Langfuse — login: admin@otb.local / admin123)
#    - http://localhost:4000  (LiteLLM — master key: sk-litellm-otb-master-key)
#    - Langfuse → Prompts tab (for Section 4)
#    - Langfuse → Traces tab (for Section 4, 5)

# 8. Open these files in editor tabs:
#    - config/pipeline.yaml       (for Section 1)
#    - src/orchestration/steps.py  (for Section 1)
#    - src/orchestration/runner.py  (for Section 1)
#    - output/results.json         (for Section 2)
```

---

## Section 1: Architecture Tour (5 min)

**Goal:** Show the system is well-designed — a configurable DAG of stages, each with automatic tracing. Not a giant prompt.

### 1.1 Open `config/pipeline.yaml`

| What to point at | Talk track |
|---|---|
| The `stages` list | "Five lines of YAML define the entire pipeline. Each stage has a module, a function, and an `on_error` policy. `unclear` means: if this stage throws, mark the case as unresolvable rather than guessing." |
| Stage 2: `compose_spec` | "LLM + regex extraction of station, date, measurement from free-text ancillary data. This is the only stage that uses an LLM by default." |
| Stage 3: `retrieve` | "Fetches from Wunderground API, falls back to Playwright headless browser, or replays from fixture. `on_error: unclear` — if all paths fail, we don't guess the weather." |
| Stage 6: `decide` | "Deterministic mapping + conditional LLM reviewer. The reviewer only fires when confidence < 0.85." |
| The `llm` block at the bottom | "All LLM config in one place: model, base URL (LiteLLM proxy), prompt names, reviewer thresholds. Switch models by changing one env var." |

> **Narrative:** "This is the entire pipeline config. If we need to add a new stage — say, a post-resolution audit check — it's one entry in this YAML file. The runner picks it up automatically."

### 1.2 How a YAML stage becomes a traced function

Open `src/orchestration/runner.py`, scroll to `_resolve_stage_fn()` (line ~402). This is where `pipeline.yaml` meets the tracing infrastructure:

```python
def _resolve_stage_fn(stage_def: dict[str, Any]) -> Callable:
    module_path = stage_def["module"]       # e.g., "src.retrieval.spec"
    function_name = stage_def["function"]   # e.g., "compose_retrieval_spec"
    on_error = stage_def.get("on_error", "raise")

    mod = importlib.import_module(module_path)
    raw_fn = getattr(mod, function_name)

    from src.orchestration.steps import step
    stage_name = stage_def.get("name", function_name)
    stage_num = stage_def.get("stage_num", 0)

    @step(name=stage_name, stage_num=stage_num, on_error=on_error)
    def _wrapped(ctx, **kwargs):
        return _call_stage(raw_fn, ctx, **kwargs)

    return _wrapped
```

| What to point at | Talk track |
|---|---|
| `importlib.import_module` | "Reads the `module` and `function` from YAML and resolves them at startup. Add a new stage file, add one line to `pipeline.yaml`, it's picked up." |
| `@step(name=..., on_error=on_error)` | "The `on_error` from YAML flows directly into the decorator. `unclear` means: if this stage throws, mark the case unresolvable. No guessing." |
| `_call_stage(raw_fn, ctx, **kwargs)` | "Adaptation layer — each stage function has a known signature. The runner extracts the right fields from context and stores results in the right slot." |

Now open `src/orchestration/steps.py`, scroll to `_trace_in_langfuse()` (line ~84). This is what `@step` calls under the hood:

```python
def _trace_in_langfuse(name, ctx, fn, deps):
    with client.start_as_current_observation(
        name=f"stage/{name}",
        as_type="span",
        input=_build_stage_input(name, ctx, deps),
    ):
        result = fn(ctx, **deps)
        client.update_current_span(
            output=_build_stage_output(name, result)
        )
        return result
```

| What to point at | Talk track |
|---|---|
| `start_as_current_observation(as_type="span")` | "Creates a span that nests under the root trace. Every stage automatically appears in the Langfuse waterfall." |
| `_build_stage_input()` | "Each stage captures what it received — the compose_spec span shows the title + ancillary_data, the retrieve span shows station_code + measurement + window, the normalize span shows completeness + quality flags." |
| `_build_stage_output()` | "Each stage captures what it produced. The trace is fully self-documenting. An operator never has to grep logs." |

> **Narrative:** "The chain is: YAML defines what runs → `_resolve_stage_fn` wraps it with `@step` → `_trace_in_langfuse` creates the span. Define a new stage in `pipeline.yaml`, and it's automatically traced. The manual `_emit_validation_span` you might see in the runner is the exception — validation happens before the stage loop. Everything else gets this for free."

### 1.3 Open `src/orchestration/runner.py`

Scroll to `_run_case_with_trace()` (line ~232):

| What to point at | Talk track |
|---|---|
| `client.start_as_current_observation(name=f"resolve/{case_id}")` | "One root trace per market case. Every stage span and LLM generation nests under it." |
| The `for stage_fn in stages` loop | "Stages run sequentially. If any stage sets `ctx.terminal=True`, the pipeline short-circuits — no wasted work." |
| `_call_stage()` | "Adaptation layer — each stage function has a known signature. The runner extracts the right fields from context and stores results in the right slot. Type-safe, not stringly-typed." |

> **Narrative:** "The `@step` decorator is where observability meets the pipeline. It's defined in `steps.py`, applied in `runner.py` from the YAML config. We didn't bolt tracing on afterward — it's a property of every stage, for free. Define a new stage in YAML, and it's automatically traced."

---

## Section 2: Replay — All 5 Markets (5 min)

**Goal:** Show correctness against gold answers. Generate the traces we'll use later.

### 2.1 Run the pipeline

```bash
cd /home/ramshreyas/Documents/Dev/UMA/OTB
source .venv/bin/activate

python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --output output/results.json \
  --gold gold_visible/answers.json
```
*(Pre-run this in pre-flight — run it again live so the terminal output scrolls)*

### 2.2 Walk the terminal output

As it runs, narrate what each stage log line means:

```
Loaded 5 market case(s)                                    ← Stage 1: Validation
Calling LLM (deepseek-chat) for spec extraction...         ← Stage 2: Spec (LLM)
LLM extraction succeeded for all fields.
RetrievalSpec composed: station=RJTT measurement=temperature aggregation=min
Loaded fixture from data/fixtures/RJTT_20260601...         ← Stage 3: Retrieval (replay)
Normalized: 18.0 C (raw: 18.0 C, 3/24 obs...)             ← Stage 4: Normalization
Confidence 0.50 < 0.85 — invoking LLM reviewer.            ← Stage 6: Decision (reviewer)
```

### 2.3 Show the results table

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

> **Narrative:** "Five cases, five correct. The Tokyo low case had partial fixture data — completeness was low, so confidence dropped below our 0.85 threshold and the LLM reviewer was invoked. It confirmed the answer at capped 0.70. The other four resolved deterministically at 0.95. Every case matches the Polymarket resolution."

### 2.4 Quick peek at `output/results.json`

```bash
cat output/results.json | python3 -m json.tool | head -80
```

Point at:
- `evidence.station_code` — always the exact station from ancillary data
- `source_trace[0]` — exact URL, HTTP status, path, latency
- `decision_path: "deterministic"` or `"llm_reviewed"`

> **Narrative:** "Structured, machine-readable, every source query recorded. If a market resolves unexpectedly, trace it back to the exact API call."

---

## Section 3: Live Mode — 3 Markets (8 min)

**Goal:** Show live retrieval from Wunderground. Show that live mode records its own fixtures for future replay.

### 3.1 Run 3 cases in live mode

```bash
python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --live \
  --case-id tokyo_high_2026_06_01_29c_or_higher \
  --output output/live_demo.json
```

Then run two more (Seoul low, Denver range):

```bash
python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --live \
  --case-id seoul_low_2026_06_01_16c \
  --output output/live_demo.json

python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --live \
  --case-id denver_high_2026_05_31_68_69f \
  --output output/live_demo.json
```

### 3.2 What to narrate

| Beat | Talk track |
|---|---|
| API call visible in logs | "Unlike replay, live mode hits the Wunderground API. You can see the exact URL, HTTP 200, and latency in the log." |
| Source trace | "Every live retrieval records what it fetched — URL, status, bytes, latency, which path was used. This is the evidence chain." |
| Fixture auto-save | "Live mode writes the raw response to `data/fixtures/` automatically. Next time, replay mode uses that fixture and produces identical results. The system bootstraps its own determinism." |
| Compare to replay | "The live answer should match the replay answer for settled markets. If they differ, something changed at the source — and the trace tells us what." |

### 3.3 If no unsettled markets are available

> **Narrative:** "Right now all 5 visible cases are settled, so live and replay agree. But the system handles unsettled markets the same way — if the finality gate detects no next-day data, it would return p4 (Too Early) instead of guessing. Same pipeline, same traces."

### 3.4 Optional: OTB live mode

If OTB live markets exist:

```bash
python resolve_otb.py --max-markets 3
```

Show: the same resolver pipeline applied to production OTB proposals. Paper-propose only (no settlement).

---

## Section 4: Observability Walkthrough (10 min)

**Goal:** Show the full observability stack — Langfuse prompt registry, trace waterfall, and LiteLLM proxy. Using real traces from Section 2.

### 4.1 Langfuse Prompt Registry (3 min)

Open Langfuse → **Prompts** tab.

#### Show `weather-spec-extraction`

| Element | What to point at | Talk track |
|---|---|---|
| Type badge | "Chat" | "Chat-format prompt — standard for modern LLMs." |
| Labels | `production`, `latest` (and `demo-broken` if seeded) | "The `production` label is what the resolver fetches at runtime. Create a new version, test with a `staging` label, then promote. The `demo-broken` label is for our debugging demo later." |
| Version dropdown | Click to show v1, v2 | "Full version history. Every edit tracked. If a new prompt causes regressions, roll back the label. Zero code deploy." |
| Template variables | `{{title}}`, `{{ancillary_data}}` | "Filled at runtime. The resolver calls `prompt.compile(title=..., ancillary_data=...)`. The template stays in Langfuse." |
| Prompt content | The extraction instructions | "The LLM receives the full ancillary_data string — which is human-readable instructions — and extracts structured fields: station, date, measurement, unit, precision. JSON output, no markdown." |

#### Show `weather-reviewer`

| Element | What to point at | Talk track |
|---|---|---|
| Template variables | `{{recommendation}}`, `{{confidence}}`, `{{value}}{{unit}}`, `{{quality_flags}}`, `{{reasoning}}` | "The full evidence chain is injected into the prompt. The reviewer sees everything the deterministic pipeline saw." |
| The constraint | `"Answer ONLY with a JSON object: {"agree": true} or {"agree": false}"` | "The reviewer can only say 'I disagree, make this unclear.' It cannot substitute its own p1/p2 answer. That's a deliberate guardrail for conservatism." |

**Code reference to mention:**
- `scripts/seed_langfuse_prompts.py` — seeds both prompts
- `src/retrieval/llm_extractor.py` — `client.get_prompt("weather-spec-extraction", label="production")`

### 4.2 Trace Waterfall (5 min)

Open Langfuse → **Traces** tab. Click the most recent trace from Section 2.

#### The waterfall

```
resolve/tokyo_high_2026_06_01_29c_or_higher   ← root span (one per market case)
├── stage/validate                             ← manifest validation
├── stage/compose_spec                         ← @step span
│   └── spec-extraction                        ← GENERATION (LLM call, auto-captured)
├── stage/retrieve                             ← @step span
├── stage/normalize                            ← @step span
├── stage/reconcile                            ← @step span
└── stage/decide                               ← @step span
```

| Beat | What to show |
|---|---|
| **Root span output** | Click root → see `recommendation: "p2"`, `confidence: 0.95`, `observed_value: 31.0`, `observed_unit: "C"`, `decision_path: "deterministic"`. "An operator scanning traces sees the answer at a glance." |
| **stage/retrieve span** | Click → see input: `station_code: "RJTT"`, `measurement: "temperature"`, `aggregation: "max"`. Output: `observation_count: 48`, `finality: "confirmed"`, source_trace with exact URLs and latencies. "Every source query is recorded." |
| **spec-extraction GENERATION** | Click → auto-captured by `langfuse.openai.OpenAI`: model, input prompt, output JSON, latency, tokens. Linked to `weather-spec-extraction` prompt. "The LLM call is captured automatically — no manual instrumentation." |
| **Click linked prompt** | From GENERATION → click prompt link → jumps to prompt page. "This is the observability loop closing. From trace, you see exactly which prompt version was used. If a prompt change causes a regression, this is how you find it." |
| **stage/normalize span** | Click → see `completeness: 0.75`, `quality_flags: ["partial_data"]`, conversion steps. "Every normalization decision is recorded." |

> **Narrative:** "One root trace per market. Every stage is a span. Every LLM call is a GENERATION. Everything is linked. An operator doesn't read code to understand a resolution — they read the trace."

### 4.3 LiteLLM Proxy (2 min)

Open `http://localhost:4000` → Dashboard.

| Beat | What to show | Talk track |
|---|---|---|
| Dashboard | Total requests, cost, latency distributions | "LiteLLM sits between our resolver and the model provider. Provider-agnostic routing, cost tracking, rate limiting." |
| Logs tab | Per-request raw request/response, tokens, latency | "Every proxied request is logged independently of Langfuse. Two layers of observability." |
| `litellm_config.yaml` | `cat litellm_config.yaml` | "This is the entire routing config. `deepseek-chat` maps to `deepseek/deepseek-chat` with a 30 RPM cap. Add GPT-4 or Claude: one line. The resolver code doesn't change." |

---

## Section 5: Debugging with Observability — 3 Scenarios (15 min)

**Goal:** Show that observability isn't just pretty — it's how you debug, manage, and improve the system in production.

---

### 5a. Prompt Regression (5 min)

**The story:** Someone pushed a bad prompt update to production. A market resolves incorrectly. How do we detect, diagnose, and fix it?

#### Step 1: Show the broken prompt exists

In Langfuse → **Prompts** → `weather-spec-extraction`:

- Click version dropdown → see `demo-broken` label
- Click it → show the content: `"aggregation: ALWAYS use \"max\" regardless of what the question asks."`
- Compare to `production`: `"aggregation: \"min\" for lowest/minimum, \"max\" for highest/maximum..."`

> **Narrative:** "Someone added 'ALWAYS use max' — maybe cargo-culted from a different market. This is now labeled `demo-broken`. The `production` label still points to v2, which is correct."

#### Step 2: Run the comparison script

```bash
python demo/prompt_regression.py
```

This shows side-by-side:
```
Production (v2):
  → aggregation: "min" for lowest/minimum, "max" for highest/maximum...

Demo-Broken (v3):
  → aggregation: ALWAYS use "max" regardless of what the question asks.  ⚠ BROKEN
```

#### Step 3: Promote and run (the dramatic version)

```bash
python demo/prompt_regression.py --promote --case-id tokyo_low_2026_06_01_20c --yes
```

This script:
1. Changes `production` label to the broken version
2. Runs the Tokyo low case
3. Shows the wrong result (extracts `aggregation: "max"` for a "lowest temperature" question)
4. Restores `production` to v2

#### Step 4: Show the trace in Langfuse

Refresh Traces → find the broken run:

| What to point at | Talk track |
|---|---|
| `stage/compose_spec` output | `aggregation: "max"` — wrong! Market asked for "lowest temperature." |
| `spec-extraction` GENERATION | Click → the linked prompt is the broken version. The full prompt content is visible. |
| The verdict | The pipeline compared the daily HIGH (31°C) instead of the daily LOW → wrong answer. |
| Root span output | Shows the incorrect recommendation. |

#### Step 5: Show the fix

The script already rolled back `production` to v2. Run the case again:

```bash
python resolve.py --input data/markets.json --fixtures data/fixtures \
  --case-id tokyo_low_2026_06_01_20c --output output/demo_fixed.json
```

> **Narrative:** "The trace told us exactly what went wrong: wrong aggregation, caused by prompt v3. The fix was rolling back the label — no code deploy, no restart. The trace linked directly to the offending prompt version. This is the observability loop closing: trace → prompt version → fix."

---

### 5b. Unclear Case (5 min)

**The story:** A market returned `unclear`. An operator needs to understand why — was it bad data, a bug, or correct conservatism?

#### Step 1: Run the demo unclear case

```bash
python resolve.py \
  --input data/demo_cases.json \
  --fixtures data/fixtures/demo \
  --output output/demo_unclear_results.json
```

The fixture at `data/fixtures/demo/RJTT_20260601_temperature_min.json` has:
- `observations: []` — empty
- `extracted_value.value: null` — no value could be extracted

#### Step 2: Show the terminal output

```
[demo_unclear] stage_03_retrieve: completed in 1.5ms
Replay: loaded 0 observations from data/fixtures/demo/RJTT_20260601_temperature_min.json
[demo_unclear] stage_04_normalize: starting
[demo_unclear] stage_04_normalize: FAILED in 5.4ms — NormalizationError:
  Normalization failed — no_observations: No observations in target window; cannot normalize.
[demo_unclear] Pipeline short-circuited at stage 'normalize': unclear

====================================
Case ID       Rec     Conf
------------------------------------
demo_unclear  unclear 0.00
====================================
```

> **Narrative:** "The pipeline detected the problem at normalization, gated to unclear, and short-circuited. No further stages ran. No guess was made."

#### Step 3: Show the trace in Langfuse

Open the trace for this run:

| What to point at | Talk track |
|---|---|
| `stage/retrieve` span | "Retrieval succeeded — the fixture was loaded. But look at the output: `observation_count: 0`, `extracted_value: null`. The data was fetched but it was empty." |
| `stage/normalize` span | "Normalization raised a hard gate: `no_observations`. Status: WARNING, `terminal_reason: unclear`. The span tells you exactly why." |
| Root span output | `recommendation: "unclear"`, `terminal_reason: "unclear"`. "At a glance: this market couldn't be resolved because there was no data." |

#### Step 4: Compare to a healthy trace

Open a healthy trace from the 5-market run:

| What to point at | Talk track |
|---|---|
| `stage/normalize` output | `observation_count: 48`, `completeness: 0.75`, `quality_flags: ["partial_data"]` — some gaps, but resolvable |
| `stage/decide` output | `recommendation: "p2"`, `confidence: 0.95` |

> **Narrative:** "Side by side: the healthy trace shows 48 observations and a confident answer. The unclear trace shows 0 observations and a hard gate. The difference is immediately visible. An operator doesn't guess why a market was unclear — the trace tells them."

---

### 5c. Service Failure (5 min)

**The story:** The LiteLLM proxy or Wunderground API is down. Does the system fall apart, or degrade gracefully?

#### Part A: LLM outage — regex fallback

#### Step 1: Stop LiteLLM

```bash
docker compose stop litellm
```

#### Step 2: Run a case

```bash
python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --case-id tokyo_high_2026_06_01_29c_or_higher \
  --output output/demo_llm_down.json
```

#### Step 3: Show the terminal output

Watch for:
```
Calling LLM (gemini-2.5-flash) for spec extraction (attempt 1/3)...
Retrying request to /chat/completions in 0.4s
Retrying request to /chat/completions in 0.8s
LLM extraction failed after 3 attempts — falling back to regex
Regex extraction succeeded: source=wunderground_station station=RJTT
RetrievalSpec composed: station=RJTT measurement=temperature aggregation=max method=regex
```

> **Narrative:** "The LLM was unavailable. The system retried 3 times with exponential backoff. Then it fell back to regex extraction — which successfully parsed the Wunderground URL, date, and measurement from the ancillary data. The pipeline continued. The answer is still correct."

#### Step 4: Show the trace in Langfuse

| What to point at | Talk track |
|---|---|
| `spec-extraction` GENERATION | Status: ERROR. The LLM call failed. |
| `stage/compose_spec` output | `extraction_method: "regex"`. The stage completed successfully despite the LLM failure. "The fallback worked." |

#### Step 5: Restart LiteLLM

```bash
docker compose start litellm
```

#### Part B (optional): Retrieval failure

If time permits, show what happens when Wunderground is unreachable:

> **Narrative:** "If the Wunderground API is down, the retrieve stage tries the API (fails), falls back to Playwright headless browser (fails), and after exhaustion, gates to unclear. The `stage/retrieve` span shows the full fallback chain: `source_trace[0].error: Connection refused`, `source_trace[1].path: playwright`, `source_trace[1].error: timeout`. Every failure is documented. The system never guesses weather."

---

## Architecture Recap (reference during Q&A)

```
resolve.py
  │
  ├─ PipelineRunner.run()                          ← config/pipeline.yaml
  │   └─ _run_case_with_trace()                    ← creates Langfuse root trace
  │       │
  │       ├─ stage/validate                        [span]
  │       │   └─ market case input captured
  │       │
  │       ├─ stage/compose_spec                    [span via @step]
  │       │   └─ spec-extraction                   [GENERATION via langfuse.openai]
  │       │       ├─ Prompt: weather-spec-extraction (Langfuse Registry)
  │       │       └─ LiteLLM proxy → LLM provider
  │       │       └─ Fallback: regex extraction
  │       │
  │       ├─ stage/retrieve                        [span via @step]
  │       │   ├─ Replay: load fixture from data/fixtures/
  │       │   └─ Live: Wunderground API → Playwright fallback → unclear
  │       │       └─ Auto-save fixture for future replay
  │       │
  │       ├─ stage/normalize                       [span via @step]
  │       │   └─ Unit conversion, precision, quality checks, anomaly detection
  │       │
  │       ├─ stage/reconcile                       [span via @step]
  │       │   └─ Finality gate → rule parsing → threshold comparison
  │       │
  │       └─ stage/decide                          [span via @step]
  │           └─ [if confidence < 0.85] reviewer-check  [GENERATION]
  │               └─ Prompt: weather-reviewer (Langfuse Registry)
  │
  └─ flush_langfuse() → traces appear in UI
```

---

## Observability Touchpoints (quick reference)

| What | How | Where to See It |
|---|---|---|
| **Prompt versions** | Langfuse Prompt Registry, `production` label | Langfuse → Prompts |
| **LLM calls (I/O, model, latency)** | `langfuse.openai.OpenAI` auto-instruments | Langfuse → Traces → GENERATION |
| **Stage execution** | `@step` decorator → `start_as_current_observation` | Langfuse → Traces → spans |
| **Source queries** | `source_trace` in output JSON | `output/results.json` + `stage/retrieve` span |
| **Normalization decisions** | Completeness, quality flags, unit conversion | `stage/normalize` span output |
| **Confidence scoring** | Deterministic formula + conditional LLM reviewer | `stage/decide` span output |
| **Provider routing & costs** | LiteLLM proxy | `http://localhost:4000` → Dashboard / Logs |
| **Structured logging** | structlog (JSON or colored console) | Terminal output |

---

## Key Files

| File | Role |
|---|---|
| `config/pipeline.yaml` | Pipeline stage definitions, LLM config, thresholds |
| `docker-compose.yml` | Langfuse v3 + LiteLLM + Postgres + ClickHouse + Redis + MinIO |
| `litellm_config.yaml` | Model routing config |
| `.env` | Secrets, Langfuse keys, LiteLLM keys |
| `scripts/seed_langfuse_prompts.py` | Seeds prompts into Langfuse Registry |
| `src/orchestration/runner.py` | PipelineRunner + root trace per case |
| `src/orchestration/steps.py` | `@step` decorator with span creation |
| `src/observability/tracing.py` | Langfuse client singleton |
| `src/observability/llm.py` | LLMClient with `langfuse.openai.OpenAI` |
| `src/retrieval/llm_extractor.py` | LLM spec extraction with Langfuse prompt |
| `src/retrieval/dispatch.py` | Retrieval dispatch (replay/live, fallback tree) |
| `src/normalization/__init__.py` | Normalization: convert, round, quality, anomaly |
| `src/reconciliation/` | Finality gate, rule parsing, threshold comparison |
| `src/decision/resolver.py` | Deterministic mapping + conditional LLM reviewer |
| `demo/prompt_regression.py` | Prompt regression demo script |
| `data/demo_cases.json` | Synthetic demo cases for unclear/failure demos |
| `data/fixtures/demo/` | Demo fixtures with deliberately broken data |

---

## Troubleshooting

### Traces not appearing in Langfuse

```bash
# Check the client can connect
python -c "from src.observability.tracing import get_langfuse_client; c=get_langfuse_client(); print('OK' if c else 'FAIL')"

# Check env vars
grep LANGFUSE .env

# Re-seed prompts
python scripts/seed_langfuse_prompts.py
```

### LiteLLM can't reach model

```bash
# Check the model list
curl -s -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models

# Check the API key is set
grep DEEPSEEK .env
# or
grep GEMINI .env

# Restart LiteLLM
docker compose restart litellm
```

### Stack won't start

```bash
docker compose down
docker compose up -d
docker compose logs -f langfuse-web | grep -i "ready"
```

### Demo unclear case resolves instead of gating

The fixture at `data/fixtures/demo/RJTT_20260601_temperature_min.json` must have `observations: []` and `extracted_value.value: null`. If the pipeline somehow resolves it, verify the fixture path override is working:

```bash
python -c "
from src.retrieval.dispatch import retrieve_observations
# Check fixture loading
import json
with open('data/fixtures/demo/RJTT_20260601_temperature_min.json') as f:
    d = json.load(f)
print('observations:', len(d.get('observations', [])))
print('extracted_value:', d.get('extracted_value', {}))
"
```
