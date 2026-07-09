# OTB Weather Market Resolver

Agentic resolver for weather prediction markets within UMA's Optimistic Oracle (OTB) framework. Given a market case, the resolver retrieves public weather evidence (Wunderground, NOAA), reconciles it against market rules, and returns **p1** (No), **p2** (Yes), **p3** (50/50), **p4** (Too Early), or **unclear**.

Built for the OTB Weather Market Resolution Case Study. See [`docs/case-study.md`](docs/case-study.md) for the full brief.

## Quick Start

```bash
# 1. Clone and set up
git clone <repo-url> && cd OTB
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. (Optional) Start observability stack — Langfuse + LiteLLM
docker compose up -d
python scripts/seed_langfuse_prompts.py

# 3. Replay mode — deterministic, uses fixtures
python resolve.py --input data/markets.json --fixtures data/fixtures

# 4. Evaluate against gold answers
python evaluate.py --predictions output/results.json --gold gold_visible/answers.json
```

## Requirements

- **Python** 3.11+
- **Playwright browsers** (for live mode fallback): `playwright install chromium`
- **Docker** (optional — for observability stack)

## Project Structure

```
.
├── README.md                   # This file
├── AGENTS.md                   # Project context for the pi coding agent
├── resolve.py                  # Entry point: replay, live, capture-fixtures
├── resolve_otb.py              # Entry point: live OTB API polling mode
├── evaluate.py                 # Evaluation against gold answers
├── requirements.txt            # Python dependencies
├── pyproject.toml              # Project config + pytest settings
├── docker-compose.yml          # Langfuse + LiteLLM observability stack
├── litellm_config.yaml         # LiteLLM model routing config
├── .env                        # Environment variables and secrets
│
├── data/
│   ├── markets.json            # Input manifest — 5 visible market cases
│   ├── fixtures/               # Captured weather-source snapshots for replay
│   │   ├── RJTT_20260601_temperature_min.json
│   │   ├── RJTT_20260601_temperature_max.json
│   │   ├── RKPK_20260601_temperature_max.json
│   │   ├── RKSI_20260601_temperature_min.json
│   │   ├── KBKF_20260531_temperature_max.json
│   │   ├── KBKF_20260708_temperature_max.json
│   │   └── KSEA_20260601_precipitation_sum.json
│   └── schema/
│       └── market_input.schema.json
│
├── gold_visible/
│   └── answers.json            # Expected answers for the 5 visible cases
│
├── config/
│   └── pipeline.yaml           # Pipeline stage definitions + LLM config
│
├── scripts/
│   └── seed_langfuse_prompts.py # Seed Langfuse prompt registry
│
├── docs/
│   ├── case-study.md           # Original case study brief
│   ├── design.md               # Architecture + pipeline design
│   ├── implementation.md       # Detailed implementation plan
│   ├── live-retrieval-fix.md   # Known issue: Wunderground live retrieval
│   ├── observability-demo.md   # Langfuse observability walkthrough
│   └── design.html             # Visual pipeline sequence diagram
│
├── src/
│   ├── validation/             # Schema validation, market case loading
│   ├── retrieval/              # Wunderground API, Playwright, NOAA, replay
│   ├── normalization/          # Unit conversion, rounding, quality checks
│   ├── reconciliation/         # Rule parsing, finality gate, comparison
│   ├── decision/               # Deterministic resolver + LLM reviewer
│   ├── orchestration/          # Pipeline runner, context, config
│   ├── observability/          # Langfuse tracing, structured logging
│   ├── otb/                    # OTB API client + transformation
│   └── output/                 # JSON output formatting
│
├── tests/
│   ├── conftest.py             # Shared pytest fixtures
│   ├── validation/             # Schema, loader, models tests
│   ├── retrieval/              # Retrieval and spec tests
│   └── otb/                    # OTB transform tests
│
└── output/
    ├── results.json            # Last resolver run output
    └── otb/                    # OTB live mode output
        ├── otb_cases/          # Persisted OTB manifests
        └── raw_otb_payloads/   # Raw OTB API responses
```

## Modes of Operation

### Replay Mode (Deterministic)

Uses pre-captured fixtures from `data/fixtures/`. No network calls. Produces identical results every run.

```bash
python resolve.py --input data/markets.json --fixtures data/fixtures

# Run a single case
python resolve.py --input data/markets.json --fixtures data/fixtures --case-id tokyo_low_2026_06_01_20c

# With gold comparison
python resolve.py --input data/markets.json --fixtures data/fixtures --gold gold_visible/answers.json
```

### Live Mode (Fetches from Wunderground/NOAA)

Queries live weather data. Falls back through: API → Playwright browser → exhaustion → `unclear`.

```bash
python resolve.py --input data/markets.json --fixtures data/fixtures --live

# Single case
python resolve.py --input data/markets.json --fixtures data/fixtures --live --case-id tokyo_low_2026_06_01_20c
```

### OTB Live Mode (Bonus)

Polls the OTB Oracle API for live proposed Weather markets and runs the same resolver pipeline.

```bash
# Fetch and resolve up to 10 proposed markets
python resolve_otb.py --max-markets 10

# Continuous polling every 5 minutes (Ctrl-C to stop)
python resolve_otb.py --poll --poll-interval 300 --max-markets 20

# Backtest against settled markets
python resolve_otb.py --status settled --max-markets 5
```

### Evaluation

```bash
python evaluate.py --predictions output/results.json --gold gold_visible/answers.json
```

## Architecture

The resolver is a **7-stage linear pipeline**: Validation → Spec Composition → Retrieval → Normalization → Reconciliation → Decision → Output.

| Stage | Module | Responsibility |
|---|---|---|
| 1. Validate | `src/validation/` | Load `markets.json`, validate schema, produce immutable `MarketCase` |
| 2. Compose Spec | `src/retrieval/spec.py` | Extract station, date, measurement type from ancillary data (LLM + regex) |
| 3. Retrieve | `src/retrieval/dispatch.py` | Fetch raw data: API → Playwright fallback → fixture replay |
| 4. Normalize | `src/normalization/` | Unit conversion, precision rounding, quality checks, anomaly detection |
| 5. Reconcile | `src/reconciliation/` | Finality gate → quality gate → rule parsing → threshold comparison |
| 6. Decide | `src/decision/resolver.py` | Deterministic mapping + conditional LLM reviewer |
| 7. Output | `src/output/formatter.py` | Structured JSON with recommendation, confidence, evidence, trace |

### Key Design Principles

1. **Source authority is explicit.** Never infer a station from a city name. Always use the exact Wunderground URL.
2. **Units are never assumed.** Always verify and convert explicitly. Record provenance in traces.
3. **Conservatism over confidence.** When data is ambiguous, prefer `unclear` over a wrong answer.
4. **Traces must be debuggable.** Every stage emits structured telemetry to Langfuse.
5. **Finality gates are mandatory.** Never resolve before the next-day datapoint exists.

See [`docs/design.md`](docs/design.md) for detailed architecture diagrams and [`docs/implementation.md`](docs/implementation.md) for the full implementation plan.

## Observability Stack (Optional)

The project includes a self-contained observability stack for tracing:

```bash
# Start Langfuse (port 3000) + LiteLLM proxy (port 4000)
docker compose up -d

# Seed prompts into Langfuse
source .venv/bin/activate
python scripts/seed_langfuse_prompts.py

# Open Langfuse UI
open http://localhost:3000   # Login: admin@otb.local / admin123

# LiteLLM API
curl -s -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models
```

- **Langfuse** (port 3000): Prompt registry, trace viewer, cost tracking
- **LiteLLM proxy** (port 4000): Provider-agnostic LLM routing

See [`docs/observability-demo.md`](docs/observability-demo.md) for a full walkthrough.

## Configuration

All pipeline settings are in `config/pipeline.yaml`. LLM configuration uses environment variables from `.env`:

```env
# Required for LLM features (spec extraction, reviewer)
DEEPSEEK_API_KEY=sk-...

# LangFuse (optional — set to enable tracing)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000

# LiteLLM proxy (optional)
LITELLM_BASE_URL=http://localhost:4000/v1
LITELLM_API_KEY=sk-litellm-otb-master-key
LITELLM_MODEL=deepseek-chat
```

## Market Cases

The `data/markets.json` manifest contains 5 visible cases:

| Case ID | Market | Station | Measurement |
|---|---|---|---|
| `tokyo_low_2026_06_01_20c` | Tokyo low == 20°C on June 1 | RJTT (Haneda) | Daily min |
| `tokyo_high_2026_06_01_29c_or_higher` | Tokyo high ≥ 29°C on June 1 | RJTT (Haneda) | Daily max |
| `busan_high_2026_06_01_22c_or_below` | Busan high ≤ 22°C on June 1 | RKPK (Gimhae) | Daily max |
| `seoul_low_2026_06_01_16c` | Seoul low == 16°C on June 1 | RKSI (Incheon) | Daily min |
| `denver_high_2026_05_31_68_69f` | Denver high between 68-69°F on May 31 | KBKF (Buckley SFB) | Daily max |

## Testing

```bash
# All tests
pytest tests/ -v

# Single layer
pytest tests/validation/ -v

# Exclude LLM tests (no API calls)
pytest tests/ -v -m "not llm"

# With coverage
pip install pytest-cov && pytest tests/ --cov=src --cov-report=term-missing
```

## Error Handling & Conservatism

| Scenario | Resolution |
|---|---|
| Fixture missing for case_id | `unclear` |
| Wunderground API blocked / timeout | `unclear` (retrieval exhaustion) |
| Next-day data not yet published | `p4` (finality gate) |
| Station returns partial day | `unclear` (incomplete data) |
| Units ambiguous or conflicting | `unclear` |
| LLM providers disagree (no quorum) | `unclear` |
| Question wording doesn't match patterns | `unclear` |

## Troubleshooting

### Live retrieval fails

Wunderground may block API access or show cookie consent dialogs. See [`docs/live-retrieval-fix.md`](docs/live-retrieval-fix.md) for the Playwright consent-handling fix.

### Langfuse not connecting

```bash
# Check all services are up
docker ps --format "table {{.Names}}\t{{.Status}}"

# Check Langfuse health
curl -s http://localhost:3000/api/public/health
```

### Import errors after updates

```bash
pip install -e .
```

## Documentation

- [`docs/case-study.md`](docs/case-study.md) — Original case study brief
- [`docs/design.md`](docs/design.md) — Architecture, pipeline design, failure modes
- [`docs/implementation.md`](docs/implementation.md) — Detailed implementation plan
- [`docs/live-retrieval-fix.md`](docs/live-retrieval-fix.md) — Known Wunderground retrieval issues
- [`docs/observability-demo.md`](docs/observability-demo.md) — Langfuse observability walkthrough
- [`AGENTS.md`](AGENTS.md) — Project context for AI coding assistants

## License

Proprietary — OTB/UMA hiring case study.
