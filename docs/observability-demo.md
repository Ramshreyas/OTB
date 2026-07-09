# OTB Weather Resolver — Observability Demo Walkthrough

> Screenshare guide. Follow section by section. Estimated duration: 12 minutes.

---

## Pre-flight (run before starting screenshare)

```bash
cd /home/ramshreyas/Documents/Dev/UMA/OTB

# 1. Confirm stack is running
docker ps --format "table {{.Names}}\t{{.Status}}"
# Expected: langfuse-web, langfuse-worker, litellm, postgres, clickhouse, redis, minio — all Up

# 2. Langfuse health
curl -s http://localhost:3000/api/public/health
# → {"status":"OK","version":"3.205.0"}

# 3. LiteLLM health
curl -s -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models
# → {"data":[{"id":"deepseek-chat",...}]}

# 4. Confirm prompts are seeded
source .venv/bin/activate
python scripts/seed_langfuse_prompts.py
# → ✓ Created prompt: weather-spec-extraction (label: production)
# → ✓ Created prompt: weather-reviewer (label: production)

# 5. Open these in browser tabs before the demo:
#    - http://localhost:3000  (Langfuse — login: admin@otb.local / admin123)
#    - http://localhost:4000  (LiteLLM — master key: sk-litellm-otb-master-key)
```

---

## Part 1: Langfuse Prompt Registry (4 min)

**Goal:** Show that prompts are managed, versioned assets — not hardcoded strings.

### 1.1 Open Langfuse → Prompts tab
- URL: `http://localhost:3000`
- Login: `admin@otb.local` / `admin123`
- Left sidebar → **Prompts**

### 1.2 Show `weather-spec-extraction`
Click the prompt row. Walk through:

| Element | What to point at | Talk track |
|---|---|---|
| **Type** | "Chat" badge | "This is a chat-format prompt — the standard for modern LLMs." |
| **Labels** | `production`, `latest` | "The `production` label is what our resolver actually fetches at runtime. We can create new versions, test them with a `staging` label, then promote." |
| **Version dropdown** | Click to show v1, v2 | "Full version history. Every edit is tracked. If a new prompt version causes regressions, we roll back the label." |
| **Template variables** | `{{title}}`, `{{ancillary_data}}` | "These are filled at runtime. The prompt template stays in Langfuse; the resolver calls `prompt.compile(title=..., ancillary_data=...)`." |
| **Prompt content** | The JSON extraction instructions | "The prompt tells the LLM to extract structured fields: station code, measurement type, unit, timezone, precision. No markdown, no fluff — just JSON." |

> **Narrative:** "Instead of burying prompts inside Python files, we manage them in Langfuse's Prompt Registry. The resolver fetches whatever version has the `production` label. If we need to add a new measurement type — say, `wind_gust` — we create v3, test it, promote the label. Zero code deploys."

### 1.3 Show `weather-reviewer`
Click back to list, then click `weather-reviewer`:

| Element | What to point at |
|---|---|
| **Template variables** | `{{recommendation}}`, `{{confidence}}`, `{{value}}{{unit}}`, `{{quality_flags}}`, `{{reasoning}}` — the full evidence chain |
| **The constraint** | "Answer ONLY with a JSON object: `{"agree": true}` or `{"agree": false}`" |
| **Design intent** | "This runs only when confidence < 0.85. It can only escalate to `unclear`, never flip p1→p2." |

> **Narrative:** "This is our safety valve. When the deterministic pipeline isn't confident — say 0.65 — we ask a second LLM to review the full evidence chain. But crucially, it can only say 'I disagree, make this unclear.' It can't substitute its own answer. That's a deliberate guardrail for conservatism."

**Code references to mention:**
- `scripts/seed_langfuse_prompts.py` — seeds both prompts
- `src/retrieval/llm_extractor.py:85` — `client.get_prompt("weather-spec-extraction")`
- `src/decision/reviewer.py:88` — `client.get_prompt("weather-reviewer")`

---

## Part 2: Live Pipeline Run (2 min)

**Goal:** Show a real execution with structured output — everything that feeds the traces.

### Run command

```bash
cd /home/ramshreyas/Documents/Dev/UMA/OTB
source .venv/bin/activate

python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --live \
  --case-id tokyo_low_2026_06_01_20c \
  --output output/demo_results.json
```

### What to narrate as it runs

Watch the log output. Point at each line:

```
stage_02_compose_spec: starting
Calling LLM (deepseek-chat) for spec extraction...
LLM extraction succeeded for all fields.
RetrievalSpec composed: station=RJTT measurement=temperature aggregation=min
  ↑ The LLM extracted: Tokyo → Haneda airport (RJTT), daily low, Celsius

stage_03_retrieve: starting
Fetching Wunderground observations: RJTT 20260601-20260601
Observations: 48 raw → 48 after timezone filter (Asia/Tokyo)
Checking finality: RJTT 20260602 → confirmed
  ↑ Two API calls: primary data + next-day finality check

stage_04_normalize: 20.0 C (48 obs, completeness=2.00)
stage_05_reconcile: ≤1ms
stage_06_decide: ≤1ms, confidence 0.95 → no reviewer needed
```

### Show the results table

```
================================================
Case ID                   Rec     Conf
------------------------------------------------
tokyo_low_2026_06_01_20c  p2     0.95
================================================
```

> **Narrative:** "Seven stages, ~3.4 seconds. This case: Tokyo minimum temperature, Haneda airport recorded exactly 20.0°C on June 1. The market asked 'will it be 20°C?' — Yes → p2. Confidence is 0.95, above our 0.85 threshold, so the LLM reviewer was never invoked. That saved a round-trip."

### Show the JSON output

```bash
cat output/demo_results.json | python3 -m json.tool | head -50
```

Point at:
- `evidence.station_code: "RJTT"` — not "Tokyo city"
- `evidence.observed_value: 20.0` + `observed_unit: "C"`
- `source_trace[0]` — exact Weather.com API URL, HTTP 200, latency in ms
- `source_trace[1]` — finality check with `guardrail_flags: ["finality_check"]`
- `decision_path: "deterministic"` — no LLM reviewer invoked

> **Narrative:** "Every source query is recorded: exact URL, HTTP status, latency, which path was used. If a market resolves unexpectedly, an operator can trace it back to the exact API call and response. No mystery."

---

## Part 3: Langfuse Traces (5 min)

**Goal:** Show the trace waterfall — every stage, every LLM call, nested hierarchically.

### 3.1 Navigate to Traces
- Langfuse left sidebar → **Traces**
- Click the most recent trace: **`resolve/tokyo_low_2026_06_01_20c`**

### 3.2 Show the waterfall

The trace view shows nested spans:

```
resolve/tokyo_low_2026_06_01_20c    ← root span (one per market case)
├── stage/compose_spec              ← span from @step decorator
│   └── spec-extraction             ← GENERATION (LLM call, auto-captured)
├── stage/retrieve                  ← span
├── stage/normalize                 ← span
├── stage/reconcile                 ← span
└── stage/decide                    ← span
```

> **Narrative:** "One root trace per market case. Under it, each pipeline stage is a span — created by our `@step` decorator. The LLM call automatically appears as a GENERATION observation nested inside the compose_spec stage."

### 3.3 Drill into `spec-extraction` (the GENERATION)

Click the `spec-extraction` row. Walk through:

| Field | What it shows |
|---|---|
| **Name** | `spec-extraction` — passed as `generation_name="spec-extraction"` in our `LLMClient.complete()` call |
| **Model** | `deepseek-chat` — routed through LiteLLM proxy |
| **Input** | The compiled prompt: title + ancillary data |
| **Output** | `{"station_code": "RJTT", "measurement": "temperature", "aggregation": "min", "unit": "C", ...}` |
| **Latency** | ~1.9s |
| **Linked Prompt** | `weather-spec-extraction` (production) — clickable link back to the prompt |

> **Narrative:** "This is where the Langfuse OpenAI integration shines. By using `langfuse.openai.OpenAI` instead of the plain OpenAI client, every LLM call is automatically captured: input, output, model, latency, and prompt linking. We didn't write any manual instrumentation for this — it's the drop-in wrapper."

### 3.4 Click linked prompt

Click the `weather-spec-extraction` link → jumps back to the prompt page.

> **Narrative:** "This is the observability loop closing. From a trace, you can see exactly which prompt version was used for that specific LLM call. If a prompt change caused a regression, you can trace it back to the exact version."

### 3.5 Drill into `stage/retrieve`

Click the `stage/retrieve` span:

| Field | Value |
|---|---|
| **Input** | `case_id`, `station_code: "RJTT"`, `window: "..."` |
| **Output** | `observations: 48`, `source_path: ["api", "api"]` |

> **Narrative:** "The retrieval stage records how many observations were fetched and which paths succeeded. Two API calls: primary data + next-day finality check. Both used the Wunderground API path."

### 3.6 Drill into `stage/decide`

| Field | Value |
|---|---|
| **Output** | `recommendation: "p2"`, `confidence: 0.95`, `path: "deterministic"` |

> **Narrative:** "Confidence was 0.95, above our 0.85 reviewer threshold, so the LLM reviewer was not invoked. The path says 'deterministic' — no human or LLM override."

### 3.7 Show the root trace output

Scroll back up to the root trace. The output shows the full summary:
- `recommendation`, `confidence`, `decision_path`
- `observed_value: 20.0`, `observed_unit: "C"`
- `verdict` from reconciliation

> **Narrative:** "The root span captures the final answer. An operator scanning traces can see at a glance: which cases resolved, at what confidence, and by what path."

### 3.8 (If you want to show a reviewer invocation)

If you have time, run a case that triggers the reviewer:

```bash
# Find a case where confidence might be lower, or force one
python resolve.py \
  --input data/markets.json \
  --fixtures data/fixtures \
  --live \
  --output output/demo_results.json
```

Then refresh Traces and find one where the trace includes a `reviewer-check` GENERATION nested under `stage/decide`. This shows the conditional LLM reviewer in action.

---

## Part 4 (Optional): LiteLLM UI (2 min)

**Goal:** Show proxy-level observability — model routing, costs, per-request logging.

### 4.1 Open LiteLLM
- URL: `http://localhost:4000`
- Master key: `sk-litellm-otb-master-key`

### 4.2 Dashboard
- Total requests, cost tracking, latency distributions
- Per-model breakdown (currently just `deepseek-chat`)

### 4.3 Logs tab
- Every proxied request with raw request/response
- Status codes, tokens, latency

> **Narrative:** "LiteLLM sits between our resolver and the model provider. It gives us cost tracking, rate limiting, and provider-agnostic routing. If we want to add GPT-4 or Claude tomorrow, we add one line to `litellm_config.yaml` — the resolver code doesn't change. And every request is logged here, independently of Langfuse."

### 4.4 Config file

Show `litellm_config.yaml`:

```bash
cat litellm_config.yaml
```

> **Narrative:** "This is the entire routing config. `deepseek-chat` maps to `deepseek/deepseek-chat` with a 30 RPM cap. The `master_key` secures the proxy. That's it."

---

## Architecture Recap (reference during Q&A)

```
resolve.py
  │
  ├─ PipelineRunner.run()
  │   └─ _run_case_with_trace()  ← creates Langfuse root trace
  │       │
  │       ├─ stage/compose_spec    [span via @step decorator]
  │       │   └─ spec-extraction   [GENERATION via langfuse.openai.OpenAI]
  │       │       └─ LiteLLM proxy → DeepSeek
  │       │
  │       ├─ stage/retrieve        [span]
  │       │   └─ Wunderground API  [recorded in source_trace]
  │       │
  │       ├─ stage/normalize       [span]
  │       │   └─ unit conversion, precision rounding, quality checks
  │       │
  │       ├─ stage/reconcile       [span]
  │       │   └─ finality gate, threshold comparison
  │       │
  │       └─ stage/decide          [span]
  │           └─ [if confidence < 0.85] reviewer-check  [GENERATION]
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
| **Source queries** | `source_trace` in output JSON | `output/results.json` + stage/retrieve span |
| **Normalization decisions** | Completeness, quality flags, unit conversion | stage/normalize span output |
| **Confidence scoring** | Deterministic formula + conditional LLM reviewer | stage/decide span output |
| **Provider routing & costs** | LiteLLM proxy | `http://localhost:4000` → Dashboard / Logs |
| **Structured logging** | structlog (JSON or colored console) | Terminal output |

---

## Key Files

| File | Role |
|---|---|
| `docker-compose.yml` | Langfuse v3 + LiteLLM + Postgres + ClickHouse + Redis + MinIO |
| `litellm_config.yaml` | Model routing config (DeepSeek → proxy) |
| `.env` | Secrets, Langfuse keys, LiteLLM keys |
| `config/pipeline.yaml` | Pipeline stage definitions, LLM thresholds |
| `scripts/seed_langfuse_prompts.py` | Seeds prompts into Langfuse Registry |
| `src/observability/tracing.py` | Langfuse client singleton |
| `src/observability/llm.py` | LLMClient with `langfuse.openai.OpenAI` |
| `src/observability/logging.py` | structlog configuration |
| `src/orchestration/steps.py` | `@step` decorator with span creation |
| `src/orchestration/runner.py` | PipelineRunner + root trace per case |
| `src/retrieval/llm_extractor.py` | LLM-based spec extraction with prompt from Langfuse |
| `src/decision/reviewer.py` | LLM reviewer with prompt from Langfuse |

---

## Troubleshooting

**Traces not appearing in Langfuse:**
```bash
# Check the client can connect
python -c "from src.observability.tracing import get_langfuse_client; c=get_langfuse_client(); print('OK' if c else 'FAIL')"

# Check env vars
grep LANGFUSE .env

# Re-seed prompts
python scripts/seed_langfuse_prompts.py
```

**LiteLLM can't reach model:**
```bash
# Check the model list
curl -s -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models

# Check the API key is set
grep DEEPSEEK .env

# Restart LiteLLM
docker compose restart litellm
```

**Stack won't start:**
```bash
docker compose down -v   # WARNING: destroys volumes/data
docker compose up -d
docker compose logs -f langfuse-web | grep -i "ready"
```
