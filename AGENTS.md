# OTB Weather Market Resolver

Building an agentic resolver for weather prediction markets that operates within UMA's Optimistic Oracle (OTB) framework. Given a market case, the resolver retrieves public weather evidence, reconciles it against market rules, and returns `p1` (No), `p2` (Yes), `p3` (50/50), `p4` (Too Early), or `unclear`.

## Project structure

```
.
├── AGENTS.md              # This file — project context for pi
├── README.md              # Setup, install, usage for evaluators
├── OTB Weather Market Resolution Case Study.md   # Original brief
├── resolve.py             # Entry point: ingest, resolve, output
├── requirements.txt       # Python dependencies
├── data/
│   ├── markets.json       # Input manifest with market cases
│   ├── fixtures/          # Captured weather-source snapshots for replay
│   └── schema/            # Input/output JSON schemas
│
│   Fixtures are the deterministic grading anchor — replay mode uses them
│   instead of live web calls. Each fixture is a captured raw or normalized
│   source payload:
│     • Wunderground → reduced observation payload from exact station URL
│       and date (daily high/low, timestamps, metadata)
│     • NOAA → captured monthly summary payload
│   Cases map to fixtures by case_id, a small fixture manifest, or an
│   optional fixture_path field. The markets.json manifest must NOT embed
│   raw source snapshots.
├── gold_visible/
│   └── answers.json       # Expected answers for visible cases
├── src/
│   ├── validation/        # Schema validation, market case loading
│   ├── retrieval/         # Data fetching (Wunderground API, Playwright fallback)
│   ├── normalization/     # Unit conversion, precision, timezone handling
│   ├── reconciliation/    # Evidence-to-rules matching
│   ├── decision/          # p1/p2/p3/p4/unclear + confidence
│   ├── models/            # LLM provider abstraction, quorum logic
│   └── output/            # Structured JSON formatting
├── tests/
│   ├── conftest.py          # Shared fixtures (paths, helper factories)
│   └── validation/
│       ├── test_schema.py    # JSON Schema validation tests
│       ├── test_loader.py    # Manifest loading tests
│       └── test_models.py    # Immutability and model tests
└── PROGRESS.md            # Work tracking, not for AI context
```

## Technology stack

- **Language:** Python 3.11+
- **Scraping:** Wunderground internal API as primary path; Playwright (headless browser) as fallback when API is blocked or returns incomplete data
- **LLM provider:** Provider-agnostic — use a quorum of providers/models for reconciliation and decision steps once architecture is settled (not yet implemented)
- **Package manager:** pip / venv

## Architecture — separation of concerns

The resolver must have clear boundaries. Do NOT build this as one giant monolithic prompt.

### 1. Retrieval (`src/retrieval/`)

Fetches raw weather data from the authoritative source specified in the market's ancillary data. Every retrieval records:
- Exact URL and parameters queried
- Timestamp of retrieval
- Raw response or captured snapshot
- Any errors or fallback paths taken

**Wunderground strategy:**
- Primary: Reverse-engineer the station history API endpoint (e.g., `/history/daily/...` JSON endpoint)
- Fallback: Playwright headless browser navigates to the station page, toggles correct units (C/F), extracts observation table
- Record which path was used in source trace

**Key pitfalls:**
- City name ≠ station. "Denver" resolves to Buckley SFB (KBKF) in Aurora. "Seoul" resolves to Incheon Intl (RKSI). Always verify against the URL in ancillary data, not city labels.
- Wunderground UI units are per-session toggles. The API or scraper must explicitly request the correct unit.
- Daily high/low observations are distinct from intraday point readings.
- Data can change before finality; after first next-day datapoint, revisions are ignored per market rules.

### 2. Normalization (`src/normalization/`)

Transforms raw source data into market-comparable values:
- Convert between °C and °F as needed to match market units
- Round to the precision specified in market rules (whole degrees for Wunderground temp markets)
- Handle local-day boundaries (not UTC) — the market date is in the station's local timezone
- Detect and flag missing observations, partial intraday data masquerading as daily values
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
- Conservative default: a wrong confident p1/p2 is worse than returning unclear on an ambiguous case
- Optionally uses LLM quorum for edge cases (future)

## Input/output contract

### Input: `data/markets.json`

Each market object has:
- `case_id`, `polymarket_url`, `proposal_tx_hash`
- `question_data`: `question_id`, `market_id`, `title`, `proposal_time`, `outcomes` (p1/p2/p3/p4 labels)
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

# Running a single case in live mode
python resolve.py --input data/markets.json --fixtures data/fixtures --live --case-id tokyo_low_2026_06_01_20c

# Capture fixtures (replay prep — records live responses into fixtures/)
python resolve.py --capture-fixtures --input data/markets.json

# Run evaluation against gold answers
python evaluate.py --predictions output/results.json --gold gold_visible/answers.json
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
- **Logging:** Use `logging` module. Structured log records (JSON lines for production, human-readable for dev).
- **Configuration:** Environment variables for API keys (`WUNDERGROUND_API_KEY` if applicable, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). No hardcoded secrets.
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
