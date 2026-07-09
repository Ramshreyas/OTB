# OTB Weather Market Resolver

Building an agentic resolver for weather prediction markets that operates within UMA's Optimistic Oracle (OTB) framework. Given a market case, the resolver retrieves public weather evidence, reconciles it against market rules, and returns `p1` (No), `p2` (Yes), `p3` (50/50), `p4` (Too Early), or `unclear`.

## Project structure

```
.
├── AGENTS.md              # This file — project context for pi
├── README.md              # Setup, install, usage for evaluators
├── resolve.py             # Entry point: replay, live, capture-fixtures
├── resolve_otb.py         # Entry point: live OTB API polling (bonus)
├── evaluate.py            # Evaluation against gold answers
├── requirements.txt       # Python dependencies
├── pyproject.toml         # Project config + pytest settings
├── docker-compose.yml     # Langfuse + LiteLLM observability stack
├── litellm_config.yaml    # LiteLLM model routing
├── .env                   # Environment variables and secrets
│
├── data/
│   ├── markets.json       # Input manifest with market cases
│   ├── fixtures/          # Captured weather-source snapshots for replay
│   └── schema/            # Input JSON schema
│
│   Fixtures are the deterministic grading anchor — replay mode uses them
│   instead of live web calls. Each fixture is a captured raw or normalized
│   source payload:
│     • Wunderground → reduced observation payload from exact station URL
│       and date (daily high/low, timestamps, metadata)
│     • NOAA → captured monthly summary payload
│   Cases map to fixtures by case_id. The markets.json manifest must NOT embed
│   raw source snapshots.
│
├── gold_visible/
│   └── answers.json       # Expected answers for visible cases
│
├── config/
│   └── pipeline.yaml      # Pipeline stage definitions + LLM config
│
├── scripts/
│   └── seed_langfuse_prompts.py  # Seed Langfuse prompt registry
│
├── docs/
│   ├── case-study.md           # Original case study brief
│   ├── design.md               # Architecture + pipeline design docs
│   ├── implementation.md       # Detailed implementation plan
│   ├── live-retrieval-fix.md   # Known Wunderground live retrieval issues
│   ├── observability-demo.md   # Langfuse observability walkthrough
│   └── design.html             # Visual pipeline Mermaid diagram
│
├── src/
│   ├── validation/        # Schema validation, market case loading
│   ├── retrieval/         # Data fetching (Wunderground API, Playwright, NOAA)
│   ├── normalization/     # Unit conversion, precision, quality checks
│   ├── reconciliation/    # Finality gate, rule parsing, comparison
│   ├── decision/          # Deterministic resolver + LLM reviewer
│   ├── orchestration/     # Pipeline runner, context, config
│   ├── observability/     # Langfuse tracing, structured logging
│   ├── otb/               # OTB API client + market transformation
│   └── output/            # Structured JSON formatting
│
├── tests/
│   ├── conftest.py          # Shared fixtures (paths, helper factories)
│   ├── validation/          # Schema, loader, models tests
│   ├── retrieval/           # Retrieval and spec tests
│   └── otb/                 # OTB transform tests
│
└── output/
    ├── results.json          # Last resolver run output
    └── otb/                  # OTB live mode output (manifests, raw payloads)
```

## Technology stack

- **Language:** Python 3.11+
- **Scraping:** Wunderground internal API as primary path; Playwright (headless browser) as fallback when API is blocked or returns incomplete data
- **LLM provider:** LiteLLM proxy (provider-agnostic) — uses DeepSeek by default, configurable via `litellm_config.yaml`
- **Observability:** Langfuse for prompt management and trace visualization; structured logging via `structlog`
- **Package manager:** pip / venv (with `pyproject.toml`)

## Architecture — separation of concerns

The resolver uses a 7-stage linear pipeline: **Validation → Spec Composition → Retrieval → Normalization → Reconciliation → Decision → Output**. See `docs/design.md` for detailed Mermaid diagrams.

### 1. Retrieval (`src/retrieval/`)

Fetches raw weather data from the authoritative source specified in the market's ancillary data. Every retrieval records:
- Exact URL and parameters queried
- Timestamp of retrieval
- Raw response or captured snapshot
- Any errors or fallback paths taken

**Wunderground strategy:**
- Primary: Direct HTTP to Wunderground station history JSON endpoint
- Fallback: Playwright headless browser → navigate station page → toggle correct unit → extract observation table
- Replay: Load pre-captured fixture by case_id from `data/fixtures/`

**Key pitfalls:**
- City name ≠ station. "Denver" resolves to Buckley SFB (KBKF) in Aurora. "Seoul" resolves to Incheon Intl (RKSI). Always verify against the URL in ancillary data, not city labels.
- Wunderground UI units are per-session toggles. The scraper must explicitly request the correct unit.
- Daily high/low observations are distinct from intraday point readings.
- Cookie consent popups may block Playwright interactions — see `docs/live-retrieval-fix.md`.

### 2. Normalization (`src/normalization/`)

Transforms raw source data into market-comparable values:
- Convert between °C and °F as needed to match market units
- Round to the precision specified in market rules (whole degrees for Wunderground temp markets)
- Handle local-day boundaries (not UTC) — the market date is in the station's local timezone
- Detect and flag missing observations, partial intraday data masquerading as daily values
- Quality checks and anomaly detection
- For precipitation markets: 2-decimal precision, monthly totals from NOAA

### 3. Reconciliation (`src/reconciliation/`)

Matches normalized evidence to market rules:
- Parse the market question: exact threshold, comparison operator (≥, ≤, between, exact)
- Apply bracket/tie rules from ancillary data
- Check finality condition — has the first next-day datapoint been published?
- If finality not met → `p4` (Too Early)
- If source data is incomplete, ambiguous, or conflicting → `unclear`

### 4. Decision (`src/decision/`)

Produces the final structured output:
- `recommendation`: p1, p2, p3, p4, or unclear
- `confidence`: 0.0 to 1.0 (not binary — reflect genuine uncertainty)
- Deterministic mapping for most cases; LLM reviewer (via Langfuse prompts) for borderline confidence (< 0.85)
- Conservative default: a wrong confident p1/p2 is worse than returning unclear on an ambiguous case

### 5. OTB Live Mode (`src/otb/`, `resolve_otb.py`)

Bonus extension that polls the OTB Oracle API for live proposed Weather markets and runs the same resolver pipeline. Features:
- Fetch + transform OTB API items into `MarketCase` objects
- Run through the same 7-stage pipeline
- Continuous polling mode with graceful shutdown
- Persist manifests and raw payloads for replay/debugging

## Input/output contract

### Input: `data/markets.json`

Each market object has:
- `case_id`, `polymarket_url`, `proposal_tx_hash`
- `question_data`: `question_id`, `market_id`, `title`, `proposal_time`, `end_date_iso`, `outcomes` (p1/p2/p3/p4 labels)
- `ancillary_data`: Wunderground station URL, unit, precision, finality rules, bulletin board info

### Output: structured JSON per case

```json
{
  "case_id": "tokyo_low_2026_06_01_20c",
  "recommendation": "p1",
  "confidence": 0.95,
  "evidence": { "source": "...", "temperature_c": 18, ... },
  "source_trace": { "primary_url": "...", "status": 200, "path": "api", ... },
  "reasoning": "Haneda RJTT recorded a low of 18°C on June 1, which is not 20°C...",
  "review_reason": null
}
```

## Common commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Replay mode (deterministic, uses fixtures)
python resolve.py --input data/markets.json --fixtures data/fixtures

# Live mode (fetches from Wunderground, records snapshots)
python resolve.py --input data/markets.json --fixtures data/fixtures --live

# Running a single case
python resolve.py --input data/markets.json --fixtures data/fixtures --live --case-id tokyo_low_2026_06_01_20c

# Run evaluation against gold answers
python evaluate.py --predictions output/results.json --gold gold_visible/answers.json

# OTB Live mode
python resolve_otb.py --max-markets 10
python resolve_otb.py --poll --poll-interval 300 --max-markets 20

# Observability stack
docker compose up -d
python scripts/seed_langfuse_prompts.py

# Tests
pytest tests/ -v
pytest tests/ -v -m "not llm"
```

## Design principles

1. **Source authority is explicit.** Never infer a station from a city name. Always use the exact Wunderground URL in ancillary data.
2. **Units are never assumed.** Always verify and convert explicitly. Record unit provenance in traces.
3. **Conservatism over confidence.** When data is ambiguous, missing, or conflicting, prefer `unclear` over a wrong answer.
4. **Traces must be debuggable.** Every source query, normalization step, and decision should leave a trace that an operator can inspect.
5. **Live mode records its own fixtures.** This makes replay possible even when source data changes or disappears.
6. **Finality gates are mandatory.** Do not resolve a market before the next-day threshold unless the market rules explicitly allow it.

## Evaluation criteria (from brief)

- Correctness: right answer with appropriate confidence
- Conservatism: unclear > wrong-confident
- Traceability: can debug every decision from trace
- Source selection: right station, right unit, right date
- Replayability: fixtures produce same result every time
- Live retrieval quality: retrieves, records, handles failures gracefully

## Conventions

- **Python style:** Follow PEP 8. Type hints on all public functions. Docstrings in Google style.
- **Error handling:** Never swallow exceptions silently. Capture in source trace as errors. Degrade to `unclear` when retrieval fails, don't guess.
- **Logging:** Use `structlog` for structured log records (JSON lines for production, human-readable for dev).
- **Configuration:** Environment variables for API keys (`DEEPSEEK_API_KEY`, `LANGFUSE_PUBLIC_KEY`, etc.). No hardcoded secrets.
- **Testing:** pytest. Test each layer independently (retrieval with mocked HTTP, normalization with known fixtures, reconciliation with edge cases). Tests are organized under `tests/` mirroring the `src/` structure. Run with:
  ```bash
  # All tests
  pytest tests/ -v

  # Single layer
  pytest tests/validation/ -v

  # With coverage
  pip install pytest-cov && pytest tests/ --cov=src --cov-report=term-missing
  ```
  Conftest.py provides shared fixtures: `markets_json_path` (real manifest), helper factories (`make_valid_manifest`, `make_case_with_overrides`), and sample case dicts.
- **Immutability:** Once a market case is loaded, don't modify it. Build a parallel resolution data structure.
- **Timezone handling:** Use `pytz` or `zoneinfo`. Station timezone from station metadata, not from city name.

## Key domain knowledge

### Wunderground station codes
Wunderground uses a mix of ICAO codes and internal identifiers in URLs:
- RJTT = Tokyo Haneda
- RKSI = Seoul Incheon (NOT Seoul city)
- RKPK = Busan Gimhae
- KBKF = Buckley SFB, Aurora CO (NOT Denver city)
- NZWN = Wellington Intl

The URL format is: `https://www.wunderground.com/history/daily/{country}/{region}/{code}/date/{YYYY-MM-DD}`

### Finality rule (critical)
Markets cannot resolve until "the first data point for the following date has been published on the resolution source." In practice this means at least one observation timestamp for date+1 must exist on the Wunderground page. Before that threshold, the answer is `p4` (Too Early).

### Temperature precision
Wunderground markets resolve to whole degrees (integer). 68.7°F → 68°F or 69°F? The market says "whole degrees Fahrenheit," meaning the observation is rounded — but treat this carefully: if Wunderground displays `68.7°F`, does it round to `69°F` per the market? The ancillary data says "measures temperatures to whole degrees," so the station's own reported daily high/low is already whole-degree. Check the actual measurement, not a derived value.

### Why wunderground is hard
- No stable API docs — it's a public web product, not a resolution data source
- JavaScript rendering required for some data paths
- Unit toggles are session-based (cookie/localStorage)
- Data can disappear or change between queries
- Station pages can be slow or return partial data
- City names in market titles often mislead about station
