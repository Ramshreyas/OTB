# OTB Weather Resolver — Implementation Plan

> Self-contained, production-quality refactor. Orchestration, observability (Langfuse + LiteLLM), prompts, and remaining pipeline stages. Designed to be implemented in order, each section building on the last.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Infrastructure: Self-Contained Langfuse + LiteLLM](#2-infrastructure-self-contained-langfuse--litellm)
3. [Orchestration Layer](#3-orchestration-layer)
4. [Observability Layer](#4-observability-layer)
5. [LLM Abstraction Layer](#5-llm-abstraction-layer)
6. [Configuration: pipeline.yaml](#6-configuration-pipelineyaml)
7. [Refactor: Existing Stages (1–3)](#7-refactor-existing-stages-13)
8. [Implement: Stage 4 — Normalization](#8-implement-stage-4--normalization)
9. [Implement: Stage 5 — Reconciliation](#9-implement-stage-5--reconciliation)
10. [Implement: Stage 6 — Decision](#10-implement-stage-6--decision)
11. [Implement: Stage 7 — Output Formatting](#11-implement-stage-7--output-formatting)
12. [Entry Points: resolve.py + evaluate.py](#12-entry-points-resolvepy--evaluatepy)
13. [Prompt Management in Langfuse](#13-prompt-management-in-langfuse)
14. [Testing Strategy](#14-testing-strategy)
15. [Implementation Order (Checklist)](#15-implementation-order-checklist)
16. [Expected Final Project Structure](#16-expected-final-project-structure)

---

## 1. Architecture Overview

```
markets.json
    │
    ▼
┌─────────────────────────────────────────────────────┐
│                  PipelineRunner                      │
│  Reads pipeline.yaml → runs each stage per case     │
│  Stages emit spans to OpenTelemetry → Langfuse      │
│  LLM calls go through LiteLLM proxy → Langfuse      │
└─────────────────────────────────────────────────────┘
    │
    ▼
Stage 1: validate     → MarketManifest (immutable)
Stage 2: compose_spec → RetrievalSpec     (LLM: Gemini via LiteLLM)
Stage 3: retrieve     → RawObservationBatch (API → Playwright → fixture)
Stage 4: normalize    → NormalizedObservation
Stage 5: reconcile    → ReconciliationVerdict
Stage 6: decide       → Resolution (LLM reviewer if conf < 0.85)
Stage 7: format_output → JSON written to output/results.json
```

**Key design decisions:**
- Each stage is a pure function: `(PipelineContext) → PipelineContext`
- All I/O models are frozen `@dataclass` — stages produce new objects via `ctx.replace(...)`
- Errors short-circuit the pipeline: terminal gates → `p4` or `unclear`
- Langfuse traces every stage, every LLM call, every HTTP fetch
- LiteLLM proxy runs as a sidecar Docker container — any LLM provider works by adding it to `litellm_config.yaml`

---

## 2. Infrastructure: Self-Contained Langfuse + LiteLLM

### 2.1 Files to Create

Create these files in the project root:

| File | Purpose |
|---|---|
| `docker-compose.yml` | Langfuse (v3) + LiteLLM + Postgres + ClickHouse + Redis + MinIO |
| `litellm_config.yaml` | Model routing: Gemini Flash via API key |
| `.env` | All secrets and service config (already partially exists) |

### 2.2 `docker-compose.yml`

```yaml
# OTB Weather Resolver — Observability Stack
# LangFuse (port 3000) + LiteLLM Proxy (port 4000)
#
# Usage:
#   docker compose up -d
#   docker compose logs -f langfuse-web    # wait for "Ready"
#
# Then:
#   LangFuse UI:  http://localhost:3000
#   LiteLLM API:  http://localhost:4000/v1

services:
  # ── Postgres — shared by Langfuse and LiteLLM ──
  postgres:
    image: docker.io/postgres:17
    restart: always
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 3s
      timeout: 3s
      retries: 10
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-postgres}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
      POSTGRES_DB: ${POSTGRES_DB:-postgres}
      TZ: UTC
      PGTZ: UTC
    ports:
      - "127.0.0.1:5433:5432"  # 5433 to avoid conflicts with host Postgres
    volumes:
      - pgdata:/var/lib/postgresql/data

  # ── ClickHouse — Langfuse analytics ──
  clickhouse:
    image: docker.io/clickhouse/clickhouse-server
    restart: always
    user: "101:101"
    environment:
      CLICKHOUSE_DB: default
      CLICKHOUSE_USER: ${CLICKHOUSE_USER:-clickhouse}
      CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD:-clickhouse}
    volumes:
      - ch_data:/var/lib/clickhouse
      - ch_logs:/var/log/clickhouse-server
    ports:
      - "127.0.0.1:8123:8123"
      - "127.0.0.1:9000:9000"
    healthcheck:
      test: wget --no-verbose --tries=1 --spider http://localhost:8123/ping || exit 1
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 1s

  # ── Redis — Langfuse caching/queues ──
  redis:
    image: docker.io/redis:7
    restart: always
    command: >
      --requirepass ${REDIS_AUTH:-myredissecret}
      --maxmemory-policy noeviction
    ports:
      - "127.0.0.1:6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "--pass", "${REDIS_AUTH:-myredissecret}", "ping"]
      interval: 3s
      timeout: 10s
      retries: 10

  # ── MinIO — Langfuse S3-compatible storage ──
  minio:
    image: cgr.dev/chainguard/minio
    restart: always
    entrypoint: sh
    command: -c 'mkdir -p /data/langfuse && minio server --address ":9000" --console-address ":9001" /data'
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER:-minio}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:-miniosecret}
    ports:
      - "9090:9000"
      - "127.0.0.1:9091:9001"
    volumes:
      - minio_data:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 1s
      timeout: 5s
      retries: 5
      start_period: 1s

  # ── Langfuse Worker ──
  langfuse-worker:
    image: docker.io/langfuse/langfuse-worker:3
    restart: always
    depends_on:
      postgres:
        condition: service_healthy
      minio:
        condition: service_healthy
      redis:
        condition: service_healthy
      clickhouse:
        condition: service_healthy
    ports:
      - "127.0.0.1:3030:3030"
    environment: &langfuse-env
      NEXTAUTH_URL: ${NEXTAUTH_URL:-http://localhost:3000}
      DATABASE_URL: ${DATABASE_URL:-postgresql://postgres:postgres@postgres:5432/postgres}
      SALT: ${SALT:-mysalt}
      ENCRYPTION_KEY: ${ENCRYPTION_KEY:-0000000000000000000000000000000000000000000000000000000000000000}
      TELEMETRY_ENABLED: ${TELEMETRY_ENABLED:-false}
      LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES: ${LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES:-true}
      CLICKHOUSE_MIGRATION_URL: ${CLICKHOUSE_MIGRATION_URL:-clickhouse://clickhouse:9000}
      CLICKHOUSE_URL: ${CLICKHOUSE_URL:-http://clickhouse:8123}
      CLICKHOUSE_USER: ${CLICKHOUSE_USER:-clickhouse}
      CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD:-clickhouse}
      CLICKHOUSE_CLUSTER_ENABLED: ${CLICKHOUSE_CLUSTER_ENABLED:-false}
      REDIS_HOST: ${REDIS_HOST:-redis}
      REDIS_PORT: ${REDIS_PORT:-6379}
      REDIS_AUTH: ${REDIS_AUTH:-myredissecret}
      LANGFUSE_USE_AZURE_BLOB: ${LANGFUSE_USE_AZURE_BLOB:-false}
      LANGFUSE_USE_OCI_NATIVE_OBJECT_STORAGE: ${LANGFUSE_USE_OCI_NATIVE_OBJECT_STORAGE:-false}
      LANGFUSE_S3_EVENT_UPLOAD_BUCKET: ${LANGFUSE_S3_EVENT_UPLOAD_BUCKET:-langfuse}
      LANGFUSE_S3_EVENT_UPLOAD_REGION: ${LANGFUSE_S3_EVENT_UPLOAD_REGION:-auto}
      LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: ${LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID:-minio}
      LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: ${LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY:-miniosecret}
      LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: ${LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT:-http://minio:9000}
      LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: ${LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE:-true}
      LANGFUSE_S3_EVENT_UPLOAD_PREFIX: ${LANGFUSE_S3_EVENT_UPLOAD_PREFIX:-events/}
      LANGFUSE_S3_MEDIA_UPLOAD_BUCKET: ${LANGFUSE_S3_MEDIA_UPLOAD_BUCKET:-langfuse}
      LANGFUSE_S3_MEDIA_UPLOAD_REGION: ${LANGFUSE_S3_MEDIA_UPLOAD_REGION:-auto}
      LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID: ${LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID:-minio}
      LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY: ${LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY:-miniosecret}
      LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: ${LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT:-http://localhost:9090}
      LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE: ${LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE:-true}
      LANGFUSE_S3_MEDIA_UPLOAD_PREFIX: ${LANGFUSE_S3_MEDIA_UPLOAD_PREFIX:-media/}
      LANGFUSE_S3_BATCH_EXPORT_ENABLED: ${LANGFUSE_S3_BATCH_EXPORT_ENABLED:-false}
      LANGFUSE_S3_BATCH_EXPORT_BUCKET: ${LANGFUSE_S3_BATCH_EXPORT_BUCKET:-langfuse}
      LANGFUSE_S3_BATCH_EXPORT_PREFIX: ${LANGFUSE_S3_BATCH_EXPORT_PREFIX:-exports/}
      LANGFUSE_S3_BATCH_EXPORT_REGION: ${LANGFUSE_S3_BATCH_EXPORT_REGION:-auto}
      LANGFUSE_S3_BATCH_EXPORT_ENDPOINT: ${LANGFUSE_S3_BATCH_EXPORT_ENDPOINT:-http://minio:9000}
      LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT: ${LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT:-http://localhost:9090}
      LANGFUSE_S3_BATCH_EXPORT_ACCESS_KEY_ID: ${LANGFUSE_S3_BATCH_EXPORT_ACCESS_KEY_ID:-minio}
      LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY: ${LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY:-miniosecret}
      LANGFUSE_S3_BATCH_EXPORT_FORCE_PATH_STYLE: ${LANGFUSE_S3_BATCH_EXPORT_FORCE_PATH_STYLE:-true}
      LANGFUSE_INGESTION_QUEUE_DELAY_MS: ${LANGFUSE_INGESTION_QUEUE_DELAY_MS:-}
      LANGFUSE_INGESTION_CLICKHOUSE_WRITE_INTERVAL_MS: ${LANGFUSE_INGESTION_CLICKHOUSE_WRITE_INTERVAL_MS:-}
      REDIS_TLS_ENABLED: ${REDIS_TLS_ENABLED:-false}
      EMAIL_FROM_ADDRESS: ${EMAIL_FROM_ADDRESS:-}
      SMTP_CONNECTION_URL: ${SMTP_CONNECTION_URL:-}

  # ── Langfuse Web ──
  langfuse-web:
    image: docker.io/langfuse/langfuse:3
    restart: always
    depends_on:
      postgres:
        condition: service_healthy
      minio:
        condition: service_healthy
      redis:
        condition: service_healthy
      clickhouse:
        condition: service_healthy
    ports:
      - "3000:3000"
    environment:
      <<: *langfuse-env
      NEXTAUTH_SECRET: ${NEXTAUTH_SECRET:-mysecret}
      LANGFUSE_INIT_ORG_ID: ${LANGFUSE_INIT_ORG_ID:-org_otb}
      LANGFUSE_INIT_ORG_NAME: ${LANGFUSE_INIT_ORG_NAME:-otb-weather}
      LANGFUSE_INIT_PROJECT_ID: ${LANGFUSE_INIT_PROJECT_ID:-project_otb}
      LANGFUSE_INIT_PROJECT_NAME: ${LANGFUSE_INIT_PROJECT_NAME:-otb-weather-resolver}
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ${LANGFUSE_INIT_PROJECT_PUBLIC_KEY:-pk-lf-otb-dev}
      LANGFUSE_INIT_PROJECT_SECRET_KEY: ${LANGFUSE_INIT_PROJECT_SECRET_KEY:-sk-lf-otb-dev}
      LANGFUSE_INIT_USER_EMAIL: ${LANGFUSE_INIT_USER_EMAIL:-admin@otb.local}
      LANGFUSE_INIT_USER_NAME: ${LANGFUSE_INIT_USER_NAME:-Admin}
      LANGFUSE_INIT_USER_PASSWORD: ${LANGFUSE_INIT_USER_PASSWORD:-admin123}

  # ── LiteLLM Proxy ──
  litellm:
    image: ghcr.io/berriai/litellm:latest
    restart: always
    ports:
      - "4000:4000"
    volumes:
      - ./litellm_config.yaml:/app/config.yaml:ro
    command: --config /app/config.yaml --detailed_debug
    environment:
      GEMINI_API_KEY: ${GEMINI_API_KEY}

volumes:
  pgdata:
  ch_data:
  ch_logs:
  redis_data:
  minio_data:
```

### 2.3 `litellm_config.yaml`

```yaml
# LiteLLM Proxy config — routes models for the OTB Weather Resolver.
# The proxy runs on port 4000. Clients talk to http://localhost:4000/v1

model_list:
  - model_name: gemini-2.5-flash
    litellm_params:
      model: gemini/gemini-2.5-flash
      api_key: ${GEMINI_API_KEY}
      rpm: 30  # generous for single-user dev

general_settings:
  master_key: sk-litellm-otb-master-key
  database_url: "postgresql://postgres:postgres@postgres:5432/litellm"

litellm_settings:
  drop_params: true
  set_verbose: false
```

### 2.4 `.env` Updates

The OTB `.env` already has `GEMINI_API_KEY` and `HF_TOKEN`. Add these entries:

```bash
# ── LangFuse ──────────────────────────────────────
LANGFUSE_PUBLIC_KEY=pk-lf-otb-dev
LANGFUSE_SECRET_KEY=sk-lf-otb-dev
LANGFUSE_HOST=http://localhost:3000

# ── LiteLLM Proxy ──────────────────────────────────
LITELLM_BASE_URL=http://localhost:4000/v1
LITELLM_API_KEY=sk-litellm-otb-master-key
LITELLM_MODEL=gemini-2.5-flash

# ── Docker Compose ─────────────────────────────────
NEXTAUTH_URL=http://localhost:3000
NEXTAUTH_SECRET=otb-dev-secret-change-me
SALT=otb-salt
ENCRYPTION_KEY=0000000000000000000000000000000000000000000000000000000000000000

POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=clickhouse
REDIS_AUTH=myredissecret
MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=miniosecret

LANGFUSE_INIT_ORG_ID=org_otb
LANGFUSE_INIT_ORG_NAME=otb-weather
LANGFUSE_INIT_PROJECT_ID=project_otb
LANGFUSE_INIT_PROJECT_NAME=otb-weather-resolver
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-otb-dev
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-otb-dev
LANGFUSE_INIT_USER_EMAIL=admin@otb.local
LANGFUSE_INIT_USER_NAME=Admin
LANGFUSE_INIT_USER_PASSWORD=admin123
```

### 2.5 How to Launch

```bash
# Start Langfuse + LiteLLM
docker compose up -d

# Wait for Langfuse to be ready (check logs)
docker compose logs -f langfuse-web | grep -i "ready"

# Verify
curl http://localhost:3000/api/public/health
# → {"status":"OK","version":"3.x.x"}

curl -H "Authorization: Bearer sk-litellm-otb-master-key" http://localhost:4000/v1/models
# → {"data":[{"id":"gemini-2.5-flash",...}],"object":"list"}

# Shut down
docker compose down
```

---

## 3. Orchestration Layer

### 3.1 Design

The orchestration layer is the thin glue between stages — a `PipelineRunner` that sequences registered `@step` functions through a shared `PipelineContext`.

**Key constraint:** existing stage functions must NOT change signatures. The `@step` wrapper adapts them.

### 3.2 `src/orchestration/__init__.py`

```python
"""Orchestration layer — PipelineRunner, PipelineContext, @step decorator."""
```

Empty init — each module is imported explicitly.

### 3.3 `src/orchestration/context.py`

The state-carrier that flows through every stage. Frozen dataclass — stages "mutate" via `replace()`.

```python
"""PipelineContext — immutable state carrier that flows through each pipeline stage."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from src.validation.models import MarketCase
from src.retrieval.spec import RetrievalSpec
from src.retrieval.dispatch import RawObservationBatch


@dataclass(frozen=True)
class PipelineContext:
    """Immutable context passed through every pipeline stage.

    Each stage reads its input slot(s), computes its output, and returns
    a new PipelineContext with the output slot filled. If a stage fails
    fatally, it sets terminal=True and the runner stops.

    Attributes:
        case: The validated MarketCase (always present after Stage 1).
        spec: RetrievalSpec from Stage 2 (compose_spec).
        raw_batch: RawObservationBatch from Stage 3 (retrieval).
        normalized: NormalizedObservation from Stage 4 (normalization).
        verdict: ReconciliationVerdict from Stage 5 (reconciliation).
        resolution: Resolution from Stage 6 (decision).
        terminal: If True, the pipeline has short-circuited.
        terminal_reason: Why the pipeline stopped (e.g., "p4_too_early", "unclear").
        terminal_error: The exception that caused termination, if any.
        stage: Name of the last completed stage (for debugging).
    """

    # ── Stage outputs (None until the stage runs) ──
    case: MarketCase
    spec: Optional[RetrievalSpec] = None
    raw_batch: Optional[RawObservationBatch] = None
    normalized: Optional["NormalizedObservation"] = None   # forward ref
    verdict: Optional["ReconciliationVerdict"] = None      # forward ref
    resolution: Optional["Resolution"] = None              # forward ref

    # ── Terminal state ──
    terminal: bool = False
    terminal_reason: str = ""
    terminal_error: Optional[Exception] = None
    stage: str = "validate"

    def replace(self, **kwargs) -> "PipelineContext":
        """Return a new PipelineContext with the given fields replaced."""
        return replace(self, **kwargs)


# Forward references for type hints — these are imported lazily to avoid circulars.
# The actual classes are defined in their respective stage modules.
class NormalizedObservation:
    pass

class ReconciliationVerdict:
    pass

class Resolution:
    pass
```

### 3.4 `src/orchestration/steps.py`

The `@step` decorator that wraps any function with timing, error capture, and Langfuse tracing.

```python
"""@step decorator — wraps pipeline stage functions with observability."""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable, Any

from src.orchestration.context import PipelineContext

logger = logging.getLogger(__name__)


def step(
    name: str,
    stage_num: int = 0,
    *,
    on_error: str = "raise",  # "raise" | "unclear" | "p4"
):
    """Decorator that wraps a pipeline stage function with telemetry.

    The decorated function must have signature:
        (PipelineContext, **deps) -> PipelineContext

    The decorator:
    1. Creates a Langfuse span for the stage
    2. Times execution
    3. Catches exceptions → records in span + sets terminal on context
    4. Logs structured entry/exit via structlog

    Args:
        name: Human-readable stage name (e.g., "compose_spec").
        stage_num: Stage number for ordering in traces.
        on_error: How to handle exceptions.
            "raise" — re-raise the exception (default, for fatal errors).
            "unclear" — catch, log, return ctx with terminal=True, reason="unclear".
            "p4" — catch, log, return ctx with terminal=True, reason="p4_too_early".
    """
    def decorator(
        fn: Callable[..., PipelineContext],
    ) -> Callable[..., PipelineContext]:
        @functools.wraps(fn)
        def wrapper(ctx: PipelineContext, **deps: Any) -> PipelineContext:
            start = time.monotonic()
            stage_id = f"stage_{stage_num:02d}_{name}"
            logger.info("[%s] %s: starting", _case_id(ctx), stage_id)

            try:
                # Import Langfuse lazily so the decorator works without it
                result = _trace_in_langfuse(name, ctx, fn, deps)
                elapsed_ms = (time.monotonic() - start) * 1000
                logger.info(
                    "[%s] %s: completed in %.1fms (terminal=%s)",
                    _case_id(ctx), stage_id, elapsed_ms, result.terminal,
                )
                return result.replace(stage=name)
            except Exception as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                logger.error(
                    "[%s] %s: FAILED in %.1fms — %s: %s",
                    _case_id(ctx), stage_id, elapsed_ms, type(exc).__name__, exc,
                )

                if on_error == "unclear":
                    return ctx.replace(
                        terminal=True,
                        terminal_reason="unclear",
                        terminal_error=exc,
                        stage=name,
                    )
                elif on_error == "p4":
                    return ctx.replace(
                        terminal=True,
                        terminal_reason="p4_too_early",
                        terminal_error=exc,
                        stage=name,
                    )
                else:
                    raise

        return wrapper
    return decorator


def _trace_in_langfuse(
    name: str,
    ctx: PipelineContext,
    fn: Callable,
    deps: dict[str, Any],
) -> PipelineContext:
    """Wrap the function call in a Langfuse span if Langfuse is configured."""
    try:
        from langfuse import get_client
        client = get_client()
        if client is not None:
            with client.start_as_current_span(
                name=f"stage/{name}",
                input={"case_id": ctx.case.case_id, "stage": ctx.stage},
            ) as span:
                result = fn(ctx, **deps)
                span.update(output={"terminal": result.terminal, "stage": name})
                if result.terminal:
                    span.update(level="WARNING", status_message=result.terminal_reason)
                return result
    except (ImportError, Exception):
        pass
    return fn(ctx, **deps)


def _case_id(ctx: PipelineContext) -> str:
    """Extract case_id from context for logging."""
    try:
        return ctx.case.case_id
    except Exception:
        return "?"
```

### 3.5 `src/orchestration/runner.py`

The main orchestrator. Reads YAML config, sequences stages, runs them per-case.

```python
"""PipelineRunner — sequences stages through PipelineContext for each market case."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from src.orchestration.context import PipelineContext
from src.validation.loader import load_markets

logger = logging.getLogger(__name__)


@dataclass
class PipelineRun:
    """Aggregate result of a full pipeline run across all market cases.

    Attributes:
        run_id: Unique identifier for this run (UUID or timestamp).
        started_at: UTC timestamp when the run started.
        completed_at: UTC timestamp when the run finished.
        total_cases: Number of market cases in the manifest.
        results: List of PipelineContext objects (one per case, in order).
        summary: Counts per recommendation type.
    """

    run_id: str
    started_at: datetime
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total_cases: int = 0
    results: list[PipelineContext] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        """Count recommendations by type."""
        counts = {"p1": 0, "p2": 0, "p3": 0, "p4": 0, "unclear": 0}
        for ctx in self.results:
            if ctx.resolution:
                rec = ctx.resolution.recommendation
                if rec in counts:
                    counts[rec] += 1
            elif ctx.terminal:
                counts["unclear"] += 1
        return counts


class PipelineRunner:
    """Loads a pipeline YAML config and runs stages for each market case.

    Usage:
        runner = PipelineRunner.from_yaml("config/pipeline.yaml")
        run = runner.run(input_path="data/markets.json", mode="live")
        runner.write_results(run, "output/results.json")
    """

    def __init__(self, stages: list[Callable[..., PipelineContext]]):
        """Create a runner from a list of stage functions.

        Each function must accept (PipelineContext, **kwargs) and return PipelineContext.
        """
        self._stages = stages

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "PipelineRunner":
        """Build a PipelineRunner from a pipeline.yaml config file.

        The YAML file defines which modules/functions to use for each stage.
        Function references are resolved via importlib.

        Config format:
            pipeline:
              stages:
                - name: compose_spec
                  module: src.retrieval.spec
                  function: compose_retrieval_spec
                  on_error: unclear
                - name: retrieve
                  module: src.retrieval.dispatch
                  function: retrieve_observations
                  on_error: unclear
                ...
        """
        config = _load_yaml(config_path)
        stage_defs = config.get("pipeline", {}).get("stages", [])
        stages = [_resolve_stage_fn(sd) for sd in stage_defs]
        return cls(stages)

    def run(
        self,
        *,
        input_path: str | Path,
        mode: str = "live",
        fixtures_dir: str = "data/fixtures",
        case_id: str | None = None,
        **extra_kwargs: Any,
    ) -> PipelineRun:
        """Load markets.json and run all stages for each case.

        Args:
            input_path: Path to markets.json.
            mode: "live" or "replay".
            fixtures_dir: Directory for fixture files.
            case_id: If set, run only this case.
            **extra_kwargs: Passed through to stage functions (e.g., api_key).

        Returns:
            PipelineRun with results for all processed cases.
        """
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        started_at = datetime.now(timezone.utc)
        logger.info("[run %s] Loading markets from %s (mode=%s)", run_id, input_path, mode)

        # ── Stage 1: Validation (always runs first) ──
        manifest = load_markets(input_path)
        cases = manifest.markets
        if case_id:
            cases = tuple(c for c in cases if c.case_id == case_id)
            if not cases:
                raise ValueError(f"Case '{case_id}' not found in manifest.")

        # ── Run stages per case ──
        results: list[PipelineContext] = []
        for i, market_case in enumerate(cases):
            logger.info(
                "[run %s] Case %d/%d: %s",
                run_id, i + 1, len(cases), market_case.case_id,
            )
            ctx = PipelineContext(case=market_case)

            for stage_fn in self._stages:
                if ctx.terminal:
                    logger.info(
                        "[%s] Pipeline short-circuited at stage '%s': %s",
                        market_case.case_id, ctx.stage, ctx.terminal_reason,
                    )
                    break

                ctx = stage_fn(
                    ctx,
                    mode=mode,
                    fixtures_dir=fixtures_dir,
                    **extra_kwargs,
                )

            results.append(ctx)

        run = PipelineRun(
            run_id=run_id,
            started_at=started_at,
            total_cases=len(cases),
            results=results,
        )
        logger.info(
            "[run %s] Complete. %d cases: %s",
            run_id, run.total_cases, run.summary,
        )
        return run

    def write_results(self, run: PipelineRun, output_path: str | Path) -> None:
        """Write run results as structured JSON to output_path."""
        from src.output.formatter import format_results
        format_results(run, Path(output_path))


# ── Internal helpers ────────────────────────────────────────────────

def _load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML config file."""
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_stage_fn(stage_def: dict[str, Any]) -> Callable:
    """Resolve a stage function from its module.function definition.

    Each stage_def is a dict with:
        - module: Python module path (e.g., "src.retrieval.spec")
        - function: Function name in that module (e.g., "compose_retrieval_spec")
        - on_error: "raise" | "unclear" | "p4" (optional, default "raise")

    The returned callable wraps the original function with the @step decorator.
    """
    import importlib

    module_path = stage_def["module"]
    function_name = stage_def["function"]
    on_error = stage_def.get("on_error", "raise")

    mod = importlib.import_module(module_path)
    raw_fn = getattr(mod, function_name)

    # Wrap with @step decorator for observability
    from src.orchestration.steps import step
    stage_name = stage_def.get("name", function_name)
    stage_num = stage_def.get("stage_num", 0)

    # The actual wrapped function adapts raw_fn to the PipelineContext protocol
    @step(name=stage_name, stage_num=stage_num, on_error=on_error)
    def _wrapped(ctx: PipelineContext, **kwargs: Any) -> PipelineContext:
        """Adapt raw stage function to PipelineContext protocol."""
        # Determine which input slot to pass based on stage
        # This is a simple heuristic — the runner passes all kwargs through
        return _call_stage(raw_fn, ctx, **kwargs)

    return _wrapped


def _call_stage(
    raw_fn: Callable,
    ctx: PipelineContext,
    **kwargs: Any,
) -> PipelineContext:
    """Call a raw stage function and update context with its output.

    This is the adaptation layer. Each stage function has a known signature,
    and this function extracts the right inputs from the context and stores
    the output back into the correct slot.

    This can also be driven by the YAML config, but for simplicity we use
    a naming convention: the stage function name maps to the context slot.
    """
    fn_name = raw_fn.__name__

    # ── Validation (Stage 0) — special case, not per-case ──
    # This is handled by PipelineRunner.run() directly.

    # ── Stage 2: compose_retrieval_spec ──
    if fn_name in ("compose_retrieval_spec",):
        spec = raw_fn(ctx.case, **kwargs)
        return ctx.replace(spec=spec)

    # ── Stage 3: retrieve_observations ──
    if fn_name in ("retrieve_observations",):
        if ctx.spec is None:
            return ctx.replace(terminal=True, terminal_reason="unclear",
                              terminal_error=ValueError("No RetrievalSpec available"))
        batch = raw_fn(ctx.spec, **kwargs)
        return ctx.replace(raw_batch=batch)

    # ── Stage 4: normalize ──
    if fn_name in ("normalize", "normalize_observation"):
        if ctx.raw_batch is None or ctx.spec is None:
            return ctx.replace(terminal=True, terminal_reason="unclear",
                              terminal_error=ValueError("Missing raw_batch or spec"))
        normalized = raw_fn(ctx.raw_batch, ctx.spec)
        return ctx.replace(normalized=normalized)

    # ── Stage 5: reconcile ──
    if fn_name in ("reconcile",):
        if ctx.normalized is None:
            return ctx.replace(terminal=True, terminal_reason="unclear",
                              terminal_error=ValueError("Missing normalized observation"))
        verdict = raw_fn(ctx.normalized, ctx.case, ctx.spec)
        return ctx.replace(verdict=verdict)

    # ── Stage 6: decide / resolve ──
    if fn_name in ("resolve", "make_decision"):
        if ctx.verdict is None:
            return ctx.replace(terminal=True, terminal_reason="unclear",
                              terminal_error=ValueError("Missing verdict"))
        resolution = raw_fn(ctx.verdict, ctx.case, ctx.spec)
        return ctx.replace(resolution=resolution)

    # ── Stage 7: format_output — handled by write_results() ──

    raise ValueError(f"Unknown stage function: {fn_name}")
```

### 3.6 `src/orchestration/config.py`

YAML config loader with env-var interpolation (used by the runner and other components).

```python
"""Configuration loader for pipeline YAML and .env integration."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML config file with ${ENV_VAR} interpolation.

    Environment variables in the form ${VAR_NAME} or ${VAR_NAME:-default}
    are substituted at load time.

    Args:
        path: Path to the YAML file.

    Returns:
        Parsed config dict with env vars resolved.
    """
    with open(path, encoding="utf-8") as f:
        raw = f.read()

    resolved = _interpolate_env(raw)
    return yaml.safe_load(resolved)


def _interpolate_env(text: str) -> str:
    """Replace ${VAR} and ${VAR:-default} with environment variable values."""
    def _replace(match: re.Match) -> str:
        full = match.group(1)
        if ":-" in full:
            var, default = full.split(":-", 1)
            return os.environ.get(var.strip(), default.strip())
        return os.environ.get(full, "")

    # Match ${...} patterns (non-greedy, stop at first })
    return re.sub(r'\$\{([^}]+)\}', _replace, text)
```

---

## 4. Observability Layer

### 4.1 `src/observability/__init__.py`

```python
"""Observability — Langfuse client, structured logging, tracing helpers."""
```

### 4.2 `src/observability/tracing.py`

Singleton Langfuse client, configured from env vars.

```python
"""Langfuse tracing client — singleton, configured from environment."""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_langfuse_client: Optional["Langfuse"] = None


def get_langfuse_client() -> Optional["Langfuse"]:
    """Get or create the Langfuse client singleton.

    Returns None if Langfuse is not configured (missing env vars).
    """
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    host = os.getenv("LANGFUSE_HOST", "")

    if not public_key or not secret_key or not host:
        logger.info("Langfuse not configured — tracing disabled.")
        return None

    try:
        from langfuse import Langfuse
        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        logger.info("Langfuse client initialized (host=%s)", host)
        return _langfuse_client
    except ImportError:
        logger.warning("langfuse package not installed — tracing disabled.")
        return None
    except Exception as e:
        logger.warning("Failed to initialize Langfuse: %s", e)
        return None


def flush() -> None:
    """Flush any pending Langfuse events. Call before process exit."""
    client = get_langfuse_client()
    if client:
        try:
            client.flush()
        except Exception as e:
            logger.warning("Langfuse flush failed: %s", e)
```

### 4.3 `src/observability/logging.py`

Configure structlog for JSON-structured output (production) and colored console (dev).

```python
"""Structured logging with structlog — JSON for prod, console for dev."""

from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging(
    level: str = "INFO",
    json_output: bool = False,
) -> None:
    """Configure structlog for the entire application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        json_output: If True, emit JSON lines (for production/log aggregation).
            If False, emit human-readable colored output.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso")

    if json_output:
        # JSON lines for production / log aggregation
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                timestamper,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    else:
        # Colored console for development
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                timestamper,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.dev.ConsoleRenderer(colors=True),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

    # Set root logger level
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )


# Auto-configure on import if LOG_LEVEL env var is set
_log_level = os.getenv("LOG_LEVEL", "INFO")
_json = os.getenv("LOG_FORMAT", "").lower() == "json"
configure_logging(level=_log_level, json_output=_json)
```

### 4.4 `src/observability/llm.py`

LiteLLM-backed call helper. Uses the OpenAI-compatible client pointed at the LiteLLM proxy. Automatically linked to Langfuse traces via `langfuse_prompt`.

```python
"""LLM abstraction — LiteLLM proxy client with Langfuse tracing."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LLMClient:
    """OpenAI-compatible client pointed at the LiteLLM proxy.

    All calls are automatically traced by Langfuse when a langfuse_prompt
    is provided.

    Usage:
        client = LLMClient.from_env()
        prompt = client.get_prompt("weather-spec-extraction")
        compiled = prompt.compile(title="...", ancillary_data="...")
        response = client.complete(compiled, temperature=0.0)
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._openai = None  # lazily created

    @classmethod
    def from_env(cls) -> "LLMClient":
        """Create from environment variables.

        Required env vars:
            LITELLM_BASE_URL (default: http://localhost:4000/v1)
            LITELLM_API_KEY (default: sk-litellm-otb-master-key)
            LITELLM_MODEL (default: gemini-2.5-flash)
        """
        return cls(
            base_url=os.getenv("LITELLM_BASE_URL", "http://localhost:4000/v1"),
            api_key=os.getenv("LITELLM_API_KEY", "sk-litellm-otb-master-key"),
            model=os.getenv("LITELLM_MODEL", "gemini-2.5-flash"),
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def _client(self):
        """Lazy-init the OpenAI client."""
        if self._openai is None:
            from openai import OpenAI
            self._openai = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
            )
        return self._openai

    def get_prompt(self, name: str, label: str = "production") -> Any:
        """Fetch a prompt from the Langfuse Prompt Registry.

        Args:
            name: Prompt name in Langfuse (e.g., "weather-spec-extraction").
            label: Prompt label (default: "production").

        Returns:
            A Langfuse prompt object with .compile(**vars) method.
        """
        from src.observability.tracing import get_langfuse_client
        client = get_langfuse_client()
        if client is None:
            raise RuntimeError(
                "Langfuse client not available. Set LANGFUSE_PUBLIC_KEY, "
                "LANGFUSE_SECRET_KEY, and LANGFUSE_HOST."
            )
        return client.get_prompt(name, label=label)

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        langfuse_prompt: Any = None,
    ) -> dict[str, Any]:
        """Send a chat completion request via LiteLLM proxy.

        Args:
            messages: List of {"role": "...", "content": "..."} dicts.
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Maximum tokens in response.
            langfuse_prompt: Optional Langfuse prompt object for tracing.

        Returns:
            Dict with keys: content, model, usage, latency_ms.
        """
        start = time.monotonic()

        # Build kwargs — include langfuse_prompt for auto-tracing
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if langfuse_prompt is not None:
            kwargs["langfuse_prompt"] = langfuse_prompt

        resp = self._client.chat.completions.create(**kwargs)

        latency_ms = int((time.monotonic() - start) * 1000)
        choice = resp.choices[0]
        usage = resp.usage

        return {
            "content": choice.message.content or "",
            "model": resp.model,
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
            },
            "latency_ms": latency_ms,
        }


# Module-level singleton — initialized once, reused everywhere
_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Get the module-level LLMClient singleton."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient.from_env()
    return _llm_client
```

---

## 5. LLM Abstraction Layer

This is covered by `src/observability/llm.py` (above). The key pattern:

1. **LLMClient** is the single entry point for all LLM calls
2. It talks to `localhost:4000/v1` (LiteLLM proxy)
3. The LiteLLM proxy routes to Gemini (and later, any models we add to `litellm_config.yaml`)
4. Langfuse traces are automatic when passing `langfuse_prompt`
5. Prompts are fetched from Langfuse Prompt Registry, not hardcoded

The existing `src/retrieval/llm_extractor.py` must be **refactored** to use `get_llm_client()` instead of raw Gemini. See §7 for details.

---

## 6. Configuration: `pipeline.yaml`

Create `config/pipeline.yaml` at the project root:

```yaml
# OTB Weather Resolver — Pipeline Configuration
# Defines the DAG of stages. Each stage is a module.function reference.

pipeline:
  name: "otb-weather-resolver-v1"
  description: "Seven-stage pipeline: validate → spec → retrieve → normalize → reconcile → decide → output"

  stages:
    # Stage 1: Input & Validation
    # Handled directly by PipelineRunner.run() — not listed here.
    # It loads markets.json and produces immutable MarketCase objects.

    # Stage 2: Compose Retrieval Spec
    - name: compose_spec
      stage_num: 2
      module: src.retrieval.spec
      function: compose_retrieval_spec
      on_error: unclear
      description: "LLM + regex extraction of station/date/measurement from ancillary_data"

    # Stage 3: Retrieval
    - name: retrieve
      stage_num: 3
      module: src.retrieval.dispatch
      function: retrieve_observations
      on_error: unclear
      description: "Fetch from Wunderground API → Playwright fallback → fixture replay"

    # Stage 4: Normalization
    - name: normalize
      stage_num: 4
      module: src.normalization
      function: normalize
      on_error: unclear
      description: "Unit conversion, precision rounding, quality checks, anomaly detection"

    # Stage 5: Reconciliation
    - name: reconcile
      stage_num: 5
      module: src.reconciliation
      function: reconcile
      on_error: unclear
      description: "Finality gate → quality gate → parse rules → compare to threshold"

    # Stage 6: Decision
    - name: decide
      stage_num: 6
      module: src.decision.resolver
      function: resolve
      on_error: unclear
      description: "Deterministic mapping + conditional LLM reviewer"

    # Stage 7: Output Formatting
    # Handled by PipelineRunner.write_results() — not listed here.

# LLM Provider configuration
llm:
  model: "${LITELLM_MODEL:-gemini-2.5-flash}"
  base_url: "${LITELLM_BASE_URL:-http://localhost:4000/v1}"
  api_key: "${LITELLM_API_KEY:-sk-litellm-otb-master-key}"

  # Prompt names in Langfuse Prompt Registry
  prompts:
    spec_extraction: "weather-spec-extraction"
    reviewer: "weather-reviewer"

  # LLM reviewer thresholds
  reviewer:
    invoke_below_confidence: 0.85   # Only invoke LLM reviewer if confidence < 0.85
    cap_confidence_at: 0.70         # If confidence was 0.50-0.70, cap at 0.70 even if reviewer agrees
```

---

## 7. Refactor: Existing Stages (1–3)

### 7.1 Stage 1 — No changes needed

`src/validation/` is already perfect — frozen dataclasses, clean loader, schema validation. The `PipelineRunner.run()` method calls `load_markets()` directly.

### 7.2 Stage 2 — Refactor `llm_extractor.py`

**Current state:** Uses raw `google-genai` SDK with hardcoded prompt string.

**Target state:** Uses `LLMClient` (LiteLLM proxy) with prompt from Langfuse.

**File: `src/retrieval/llm_extractor.py`** — rewrite:

```python
"""LLM Extractor — Uses LiteLLM proxy to extract RetrievalSpec fields.

The prompt is fetched from Langfuse Prompt Registry (named "weather-spec-extraction").
If Langfuse is unavailable, falls back to a hardcoded default prompt.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from src.observability.llm import get_llm_client
from src.retrieval.models import TargetWindow

logger = logging.getLogger(__name__)

# ── Fallback prompt (used when Langfuse is unavailable) ──
_FALLBACK_PROMPT = """Extract these fields from the weather market resolution instructions below as a single JSON object. Return ONLY valid JSON, no markdown fences.

Required fields:
- source_type: "wunderground_station" if wunderground.com, "noaa_monthly" if weather.gov
- station_url: exact URL from the text
- station_code: 4-letter ICAO code from URL (e.g., RJTT, KBKF, RKSI)
- target_window_start: date as YYYY-MM-DD
- target_window_end: same as start for single-day markets, as YYYY-MM-DD
- measurement: one of [temperature, precipitation, wind_speed, wind_gust, humidity, visibility, pressure, snow, uv_index, cloud_cover, dew_point]
- aggregation: "min" for lowest/minimum, "max" for highest/maximum, "sum" for total/precipitation, "point" for specific timestamp
- unit: "C" for Celsius, "F" for Fahrenheit, "in" for inches, "mm" for millimeters
- precision: integer decimal places. "whole degrees" = 1. "2 decimal places" = 2.
- timezone: IANA timezone like "Asia/Tokyo", "America/Denver", "Asia/Seoul", "Pacific/Auckland"
- finality_after: YYYY-MM-DD, day after window_end

Title: {title}

Ancillary data:
{ancillary_data}"""


def create_litellm_extractor():
    """Create an LLM extractor callable backed by LiteLLM proxy + Langfuse prompts.

    Returns:
        A callable suitable for passing as ``llm_extractor`` to
        ``compose_retrieval_spec()``.
    """
    client = get_llm_client()

    def extract(ancillary_data: str, title: str) -> dict[str, object]:
        """Extract RetrievalSpec fields using LLM via LiteLLM proxy."""
        # ── Fetch prompt from Langfuse, or use fallback ──
        try:
            prompt = client.get_prompt("weather-spec-extraction", label="production")
            compiled = prompt.compile(title=title, ancillary_data=ancillary_data)
            langfuse_prompt = prompt
        except Exception:
            logger.warning("Langfuse prompt unavailable; using fallback prompt.")
            compiled = _FALLBACK_PROMPT.format(title=title, ancillary_data=ancillary_data)
            langfuse_prompt = None

        logger.info("Calling LLM (%s) for spec extraction...", client.model)

        response = client.complete(
            messages=[{"role": "user", "content": compiled}],
            temperature=0.0,
            max_tokens=2048,
            langfuse_prompt=langfuse_prompt,
        )

        raw_text = response["content"]
        logger.debug(
            "LLM response: %d chars, %dms, %d tokens",
            len(raw_text), response["latency_ms"],
            response["usage"]["completion_tokens"],
        )

        # ── Parse JSON ──
        raw_text = _strip_markdown_fences(raw_text)
        try:
            llm_result = json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error("LLM returned invalid JSON: %s", e)
            raise ValueError(f"LLM returned invalid JSON: {e}") from e

        return _normalize_result(llm_result)

    return extract


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _normalize_result(raw: dict[str, object]) -> dict[str, object]:
    """Normalize LLM JSON into the format compose_retrieval_spec expects."""
    result: dict[str, object] = {}

    for field in ("source_type", "station_url", "station_code",
                  "measurement", "aggregation", "unit"):
        val = raw.get(field)
        result[field] = str(val) if val else ""

    precision = raw.get("precision")
    result["precision"] = max(1, int(precision)) if isinstance(precision, (int, float)) else 1

    tz = raw.get("timezone")
    result["timezone"] = str(tz) if tz else "UTC"

    start_str = str(raw.get("target_window_start", ""))
    end_str = str(raw.get("target_window_end", ""))
    if start_str and end_str:
        try:
            result["target_window"] = TargetWindow(
                start=datetime.strptime(start_str, "%Y-%m-%d"),
                end=datetime.strptime(end_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59),
            )
        except (ValueError, TypeError):
            logger.warning("Could not parse LLM dates: %s / %s", start_str, end_str)

    finality_str = str(raw.get("finality_after", ""))
    if finality_str:
        try:
            result["finality_after"] = datetime.strptime(finality_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            logger.warning("Could not parse LLM finality_after: %s", finality_str)

    return result
```

**Important:** The existing `create_gemini_extractor` function must be **deleted** from `llm_extractor.py`. The new function is `create_litellm_extractor`. Update `compose_retrieval_spec()` to use this by default when no `llm_extractor` callable is passed.

### 7.3 Stage 3 — Add `@step`-compatible wrapper

The existing `retrieve_observations()` function signature is fine. We just need to make sure the `_call_stage` adapter in `runner.py` passes the right kwargs. No code changes needed in the retrieval modules themselves.

### 7.4 Remove old `google-genai` import

The `llm_extractor.py` old code imported `from google import genai`. Remove that dependency from `pyproject.toml` and `requirements.txt`. The LiteLLM proxy handles all provider connectivity.

---

## 8. Implement: Stage 4 — Normalization

### 8.1 Package: `src/normalization/`

Create the package with these modules:

| File | Purpose |
|---|---|
| `__init__.py` | `normalize()` entry point — sequences all sub-steps |
| `convert.py` | Unit conversion (°F ↔ °C, in ↔ mm, mph ↔ kph) |
| `round.py` | Precision rounding per spec |
| `verify.py` | Window-boundary re-verification (defense-in-depth) |
| `quality.py` | Completeness scoring, gap detection, flag escalation |
| `anomaly.py` | Physical-limit threshold checks |
| `models.py` | `NormalizedObservation` dataclass + `QualityFlag`, `AnomalyFlag` enums |

### 8.2 `src/normalization/models.py`

```python
"""Data models for normalization output."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class QualityFlag(str, Enum):
    """Soft flags — reduce confidence but don't gate."""
    PARTIAL_DATA = "partial_data"
    OBSERVATION_GAP = "observation_gap"
    UNIT_PROVENANCE_CONFLICT = "unit_provenance_conflict"
    NEAR_DAY_BOUNDARY = "near_day_boundary"


class AnomalyFlag(str, Enum):
    """Hard flags — gate to unclear."""
    VALUE_OUT_OF_PHYSICAL_RANGE = "value_out_of_physical_range"
    SENSOR_ERROR_SUSPECTED = "sensor_error_suspected"


@dataclass(frozen=True)
class NormalizedObservation:
    """Normalized, verified observation ready for reconciliation.

    Attributes:
        value: The normalized, converted, rounded value in the market's expected unit.
        unit: The unit this value is in — guaranteed to match RetrievalSpec.unit.
        precision: The precision this value is rounded to.
        observation_count: Number of in-window observations that contributed.
        expected_count: Expected number of observations (~24 for single day).
        completeness: Ratio of actual to expected observations (0.0 – 1.0).
        quality_flags: Soft flags that reduce confidence.
        anomaly_flags: Hard flags that gate to unclear.
        raw_value: The original value before normalization (for trace).
        raw_unit: The original unit before conversion (for trace).
    """

    value: float
    unit: str
    precision: int
    observation_count: int
    expected_count: int
    completeness: float
    quality_flags: tuple[QualityFlag, ...] = ()
    anomaly_flags: tuple[AnomalyFlag, ...] = ()
    raw_value: float = 0.0
    raw_unit: str = ""

    @property
    def is_clean(self) -> bool:
        """True if no quality or anomaly flags are set."""
        return len(self.quality_flags) == 0 and len(self.anomaly_flags) == 0

    @property
    def has_hard_anomaly(self) -> bool:
        """True if there are hard anomaly flags (gates to unclear)."""
        return len(self.anomaly_flags) > 0
```

### 8.3 `src/normalization/convert.py`

```python
"""Unit conversion — pure math, no LLM."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Conversion constants
_CONVERSIONS: dict[tuple[str, str], callable] = {}


def _register(from_unit: str, to_unit: str, fn):
    _CONVERSIONS[(from_unit, to_unit)] = fn


_register("F", "C", lambda v: (v - 32) * 5 / 9)
_register("C", "F", lambda v: v * 9 / 5 + 32)
_register("in", "mm", lambda v: v * 25.4)
_register("mm", "in", lambda v: v / 25.4)
_register("mph", "kph", lambda v: v * 1.60934)
_register("kph", "mph", lambda v: v / 1.60934)


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a value between units.

    Args:
        value: The numeric value to convert.
        from_unit: Source unit (C, F, in, mm, mph, kph).
        to_unit: Target unit.

    Returns:
        Converted value.

    Raises:
        ValueError: If the conversion is not supported.
    """
    if from_unit == to_unit:
        return value

    fn = _CONVERSIONS.get((from_unit, to_unit))
    if fn is None:
        raise ValueError(
            f"No conversion from '{from_unit}' to '{to_unit}'."
        )

    result = fn(value)
    logger.debug("convert: %.4f %s → %.4f %s", value, from_unit, result, to_unit)
    return result
```

### 8.4 `src/normalization/round.py`

```python
"""Precision rounding per market spec."""

import math


def round_to_precision(value: float, precision: int) -> float:
    """Round a value to the specified decimal precision.

    Args:
        value: The value to round.
        precision: Number of decimal places (1 = whole, 2 = hundredths).

    Returns:
        Rounded value.
    """
    if precision <= 0:
        return float(round(value))

    # Use round() for standard rounding (banker's rounding).
    # For weather markets, this is fine — the station data is already
    # whole-degree, and rounding is a safety net.
    return round(value, precision)
```

### 8.5 `src/normalization/quality.py`

```python
"""Quality checks — completeness, gaps, flag escalation."""

from __future__ import annotations

import logging
from typing import Any

from src.normalization.models import QualityFlag
from src.retrieval.dispatch import RawObservationBatch

logger = logging.getLogger(__name__)


def assess_quality(
    batch: RawObservationBatch,
    expected_count: int,
) -> tuple[QualityFlag, ...]:
    """Assess observation quality and return soft flags.

    Args:
        batch: The raw observation batch from retrieval.
        expected_count: Expected number of observations for a full window.

    Returns:
        Tuple of quality flags (empty if clean).
    """
    flags: list[QualityFlag] = []

    obs_count = len(batch.observations)

    # ── Completeness check ──
    if obs_count < expected_count * 0.75:
        flags.append(QualityFlag.PARTIAL_DATA)
        logger.info("Partial data: %d/%d observations", obs_count, expected_count)

    # ── Gap detection ──
    if obs_count >= 2:
        if _has_temporal_gaps(batch.observations):
            flags.append(QualityFlag.OBSERVATION_GAP)
            logger.info("Temporal gaps detected in observations")

    # ── Escalate guardrail flags from source trace ──
    for entry in batch.source_trace:
        for gf in entry.guardrail_flags:
            if "unit" in gf.lower() and "mismatch" in gf.lower():
                if QualityFlag.UNIT_PROVENANCE_CONFLICT not in flags:
                    flags.append(QualityFlag.UNIT_PROVENANCE_CONFLICT)
            if "near_day_boundary" in gf.lower():
                if QualityFlag.NEAR_DAY_BOUNDARY not in flags:
                    flags.append(QualityFlag.NEAR_DAY_BOUNDARY)

    return tuple(flags)


def _has_temporal_gaps(observations: tuple[dict[str, Any], ...]) -> bool:
    """Check for gaps > 2 hours between consecutive observations."""
    timestamps = sorted(
        obs.get("valid_time_gmt", 0) for obs in observations
        if obs.get("valid_time_gmt") is not None
    )
    if len(timestamps) < 2:
        return False

    for i in range(len(timestamps) - 1):
        gap_s = timestamps[i + 1] - timestamps[i]
        if gap_s > 7200:  # 2 hours
            return True
    return False
```

### 8.6 `src/normalization/anomaly.py`

```python
"""Anomaly detection — physical-limit threshold checks."""

from __future__ import annotations

import logging
from typing import Optional

from src.normalization.models import AnomalyFlag

logger = logging.getLogger(__name__)

# Physical limits per measurement type
_PHYSICAL_LIMITS: dict[str, tuple[float, float]] = {
    "temperature": (-80.0, 60.0),     # °C: Vostok (−89.2) to Death Valley (56.7)
    "wind_speed": (0.0, 400.0),       # kph: Barrow Island cyclone (408)
    "wind_gust": (0.0, 500.0),        # kph: slightly higher than sustained
    "precipitation": (0.0, 3000.0),   # mm/month: Cherrapunji extreme
    "humidity": (0.0, 100.0),         # %: physical definition
    "pressure": (850.0, 1090.0),      # hPa: Typhoon Tip (870) to Siberia (1083.8)
    "visibility": (0.0, 100.0),       # km
    "snow": (0.0, 5000.0),            # mm water equivalent
    "uv_index": (0.0, 20.0),          # UV index scale
    "cloud_cover": (0.0, 100.0),      # %
    "dew_point": (-80.0, 40.0),       # °C
}


def check_anomalies(
    value: Optional[float],
    measurement: str,
    unit: str,
) -> tuple[AnomalyFlag, ...]:
    """Check if a value falls outside known physical limits.

    Args:
        value: The value to check (may be None for missing data).
        measurement: Measurement type (temperature, wind_gust, etc.).
        unit: Unit of the value (for context, not used in limit check).

    Returns:
        Tuple of anomaly flags (empty if clean).
    """
    if value is None:
        return (AnomalyFlag.SENSOR_ERROR_SUSPECTED,)

    limits = _PHYSICAL_LIMITS.get(measurement)
    if limits is None:
        # Unknown measurement — can't check, but don't flag
        logger.debug("No physical limits for measurement '%s'", measurement)
        return ()

    low, high = limits
    if value < low or value > high:
        logger.warning(
            "Anomaly: %s = %.2f %s outside [%.1f, %.1f]",
            measurement, value, unit, low, high,
        )
        return (AnomalyFlag.VALUE_OUT_OF_PHYSICAL_RANGE,)

    return ()
```

### 8.7 `src/normalization/verify.py`

```python
"""Window-boundary re-verification (defense-in-depth)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytz


def verify_window(
    observations: tuple[dict[str, Any], ...],
    window_start: datetime,
    window_end: datetime,
    timezone_str: str,
) -> bool:
    """Verify all observations fall within the target window.

    This is defense-in-depth — retrieval already filtered, but normalization
    re-verifies in case of timezone edge cases.

    Args:
        observations: Raw observation dicts with valid_time_gmt (epoch seconds).
        window_start: Window start in station-local time.
        window_end: Window end in station-local time.
        timezone_str: IANA timezone name (e.g., "Asia/Tokyo").

    Returns:
        True if all observations are in-window.
    """
    tz = pytz.timezone(timezone_str)
    all_in_window = True

    for obs in observations:
        epoch = obs.get("valid_time_gmt")
        if epoch is None:
            continue
        obs_dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(tz)

        if obs_dt < window_start or obs_dt > window_end:
            all_in_window = False
            break

    return all_in_window
```

### 8.8 `src/normalization/__init__.py`

```python
"""Normalization stage — transforms RawObservationBatch into NormalizedObservation.

Entry point: normalize(batch, spec) → NormalizedObservation
"""

from __future__ import annotations

import logging

from src.normalization.convert import convert
from src.normalization.round import round_to_precision
from src.normalization.verify import verify_window
from src.normalization.quality import assess_quality
from src.normalization.anomaly import check_anomalies
from src.normalization.models import NormalizedObservation, QualityFlag, AnomalyFlag
from src.retrieval.dispatch import RawObservationBatch
from src.retrieval.spec import RetrievalSpec

logger = logging.getLogger(__name__)


class NormalizationError(Exception):
    """Raised when normalization encounters a hard failure (gates to unclear)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"Normalization failed — {reason}: {detail}")
        self.reason = reason
        self.detail = detail


def normalize(
    batch: RawObservationBatch,
    spec: RetrievalSpec,
) -> NormalizedObservation:
    """Normalize raw observations into a form ready for reconciliation.

    Steps:
    1. Verify all observations are within the target window
    2. Convert units if needed
    3. Round to spec precision
    4. Assess quality (completeness, gaps, guardrail escalation)
    5. Check for physical anomalies

    Args:
        batch: RawObservationBatch from Stage 3 (retrieval).
        spec: RetrievalSpec from Stage 2.

    Returns:
        NormalizedObservation ready for reconciliation.

    Raises:
        NormalizationError: On hard failures (no observations, unconvertible units,
            anomalous data).
    """
    raw_value = batch.extracted_value.value
    raw_unit = batch.extracted_value.unit
    target_unit = spec.unit
    precision = spec.precision
    measurement = spec.measurement

    # ── Hard gate: at least one observation? ──
    obs_count = len(batch.observations)
    if obs_count == 0 or raw_value is None:
        raise NormalizationError(
            "no_observations",
            "No observations in target window; cannot normalize.",
        )

    # ── Step 1: Window-boundary verification ──
    in_window = verify_window(
        batch.observations,
        spec.target_window.start,
        spec.target_window.end,
        spec.timezone,
    )
    if not in_window:
        logger.warning("Some observations fall outside target window — flagged.")

    # ── Step 2: Unit conversion ──
    if raw_unit != target_unit:
        try:
            value = convert(raw_value, raw_unit, target_unit)
        except ValueError as e:
            raise NormalizationError(
                "unit_conversion",
                f"Cannot convert {raw_unit} → {target_unit}: {e}",
            ) from e
    else:
        value = raw_value

    # ── Step 3: Precision rounding ──
    value = round_to_precision(value, precision)

    # ── Step 4: Quality assessment ──
    expected_count = 24 if spec.target_window.is_single_day else 720
    quality_flags = assess_quality(batch, expected_count)

    # ── Step 5: Anomaly detection ──
    anomaly_flags = check_anomalies(value, measurement, target_unit)

    # ── Hard gate: physical anomaly? ──
    if len(anomaly_flags) > 0:
        raise NormalizationError(
            "anomalous_data",
            f"Value {value}{target_unit} outside physical limits for {measurement}. "
            f"Flags: {[f.value for f in anomaly_flags]}",
        )

    completeness = obs_count / expected_count if expected_count > 0 else 1.0

    normalized = NormalizedObservation(
        value=value,
        unit=target_unit,
        precision=precision,
        observation_count=obs_count,
        expected_count=expected_count,
        completeness=min(completeness, 1.0),
        quality_flags=quality_flags,
        anomaly_flags=(),
        raw_value=raw_value,
        raw_unit=raw_unit,
    )

    logger.info(
        "Normalized: %.1f %s (raw: %.1f %s, %d/%d obs, completeness=%.2f, "
        "quality=%s)",
        value, target_unit, raw_value, raw_unit,
        obs_count, expected_count, completeness,
        [f.value for f in quality_flags] if quality_flags else "clean",
    )

    return normalized
```

---

## 9. Implement: Stage 5 — Reconciliation

### 9.1 Package: `src/reconciliation/`

| File | Purpose |
|---|---|
| `__init__.py` | `reconcile()` entry point |
| `finality.py` | Finality gate check |
| `quality_gate.py` | Quality + anomaly gate check |
| `rule_parser.py` | Regex-based operator/threshold extraction |
| `comparator.py` | Math comparison against threshold |
| `models.py` | `ReconciliationVerdict` dataclass |

### 9.2 `src/reconciliation/models.py`

```python
"""Data models for reconciliation output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ReconciliationVerdict:
    """The result of reconciling normalized observations against market rules.

    Attributes:
        verdict: "Yes", "No", "p3_tie", "p4_too_early", or "unclear".
        gate: Which gate produced the verdict ("finality", "quality", "comparison").
        operator: Parsed operator: "exact", "gte", "lte", "range".
        threshold: Parsed threshold value(s) — single float or [low, high].
        comparison_result: True/False/None (None if gated before comparison).
        confidence_penalties: List of (flag_name, penalty_amount) tuples.
        reasoning: Concise explanation string.
        normalized_value: The value that was compared.
    """

    verdict: str                                          # "Yes" | "No" | "p3_tie" | "p4_too_early" | "unclear"
    gate: str                                             # "finality" | "quality" | "comparison"
    operator: str = ""                                    # "exact" | "gte" | "lte" | "range"
    threshold: float | tuple[float, float] | None = None  # Single or range
    comparison_result: Optional[bool] = None
    confidence_penalties: tuple[tuple[str, float], ...] = ()
    reasoning: str = ""
    normalized_value: float = 0.0

    def __post_init__(self):
        valid_verdicts = {"Yes", "No", "p3_tie", "p4_too_early", "unclear"}
        if self.verdict not in valid_verdicts:
            raise ValueError(f"verdict must be one of {valid_verdicts}")
```

### 9.3 `src/reconciliation/finality.py`

```python
"""Gate 1: Finality check."""

from __future__ import annotations

from src.retrieval.dispatch import RawObservationBatch
from src.reconciliation.models import ReconciliationVerdict


def check_finality(batch: RawObservationBatch) -> ReconciliationVerdict | None:
    """Check if the market's finality condition is met.

    If finality is not confirmed, returns a verdict of p4_too_early.
    Returns None if the gate is passed (finality confirmed).

    Args:
        batch: RawObservationBatch with finality field.

    Returns:
        ReconciliationVerdict if gate fails, None if passed.
    """
    status = batch.finality.status

    if status == "confirmed":
        return None  # Pass — proceed to gate 2

    if status == "not_yet":
        return ReconciliationVerdict(
            verdict="p4_too_early",
            gate="finality",
            reasoning="Finality gate: next-day datapoint not yet published on the resolution source.",
        )

    # status == "unknown" → conservative default: treat as p4
    return ReconciliationVerdict(
        verdict="p4_too_early",
        gate="finality",
        reasoning="Finality gate: could not verify next-day datapoint (fetch failed). "
                  "Conservative default: assume not final.",
    )
```

### 9.4 `src/reconciliation/quality_gate.py`

```python
"""Gate 2: Quality & anomaly checks."""

from __future__ import annotations

from src.normalization.models import NormalizedObservation, QualityFlag, AnomalyFlag
from src.reconciliation.models import ReconciliationVerdict


# Penalty per soft quality flag
_PENALTY_PER_FLAG = 0.08
_COMPLETENESS_PENALTY = 0.10  # if completeness < 0.75


def check_quality(normalized: NormalizedObservation) -> ReconciliationVerdict | None:
    """Check quality and anomaly flags. Returns a verdict if it must gate.

    Soft flags → accumulate confidence penalties (returned in verdict).
    Hard flags → gate to unclear.
    Completeness < 0.5 → gate to unclear.

    Args:
        normalized: NormalizedObservation from Stage 4.

    Returns:
        ReconciliationVerdict if hard gate fails,
        or a passing verdict with confidence_penalties populated.
        None if there's nothing to report (no flags at all).
    """
    # ── Hard gate: anomaly flags ──
    if normalized.has_hard_anomaly:
        return ReconciliationVerdict(
            verdict="unclear",
            gate="quality",
            reasoning=f"Hard anomaly detected: {[f.value for f in normalized.anomaly_flags]}",
        )

    # ── Hard gate: critically low completeness ──
    if normalized.completeness < 0.5:
        return ReconciliationVerdict(
            verdict="unclear",
            gate="quality",
            reasoning=f"Observation completeness too low ({normalized.completeness:.2f}); "
                      f"only {normalized.observation_count}/{normalized.expected_count} observations.",
        )

    # ── Soft flags: accumulate penalties ──
    penalties: list[tuple[str, float]] = []

    for flag in normalized.quality_flags:
        penalties.append((flag.value, _PENALTY_PER_FLAG))

    if normalized.completeness < 0.75:
        penalties.append(("low_completeness", _COMPLETENESS_PENALTY))

    if not penalties:
        return None  # Clean — no penalties

    return ReconciliationVerdict(
        verdict="",  # not set yet; will be filled by comparison
        gate="quality",
        confidence_penalties=tuple(penalties),
    )
```

### 9.5 `src/reconciliation/rule_parser.py`

```python
"""Gate 3a: Parse market rules from title text — regex-based, no LLM."""

from __future__ import annotations

import re
from typing import Optional


# Known patterns for Polymarket weather markets
_PATTERNS = [
    # "Will the lowest temperature in Tokyo be 20°C on June 1?"
    # operator: exact
    (r"be\s+(\d+(?:\.\d+)?)\s*°?\s*[CF]\s+on\b", "exact", 1, None),
    # "Will the highest temperature in Tokyo be 29°C or higher on June 1?"
    (r"be\s+(\d+(?:\.\d+)?)\s*°?\s*[CF]\s+or\s+higher", "gte", 1, None),
    # "Will the highest temperature in Tokyo be 29°C or above on June 1?"
    (r"be\s+(\d+(?:\.\d+)?)\s*°?\s*[CF]\s+or\s+above", "gte", 1, None),
    # "Will the highest temperature in Busan be 22°C or below on June 1?"
    (r"be\s+(\d+(?:\.\d+)?)\s*°?\s*[CF]\s+or\s+below", "lte", 1, None),
    # "Will the highest temperature in Denver be between 68-69°F on May 31?"
    (r"between\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*°?\s*[CF]", "range", 1, 2),
]


def parse_rules(title: str) -> dict[str, object]:
    """Parse operator and threshold from a market question title.

    Args:
        title: The question title (e.g., "Will the lowest temperature in Tokyo be 20°C on June 1?").

    Returns:
        Dict with keys: operator (str), threshold (float or [float, float]),
        parsed (bool). If parsing fails, parsed=False.
    """
    title_clean = title.strip()

    for pattern, operator, *group_nums in _PATTERNS:
        match = re.search(pattern, title_clean, re.IGNORECASE)
        if match:
            if operator == "range" and len(group_nums) >= 2:
                low = float(match.group(group_nums[0]))
                high = float(match.group(group_nums[1]))
                return {"operator": operator, "threshold": (low, high), "parsed": True}
            else:
                val = float(match.group(group_nums[0]))
                return {"operator": operator, "threshold": val, "parsed": True}

    return {"operator": "", "threshold": None, "parsed": False}
```

### 9.6 `src/reconciliation/comparator.py`

```python
"""Gate 3b: Math comparison of normalized value against threshold."""

from __future__ import annotations


def compare(value: float, operator: str, threshold: float | tuple[float, float]) -> bool:
    """Compare a normalized value against a threshold using the given operator.

    Args:
        value: The normalized observation value.
        operator: "exact", "gte", "lte", or "range".
        threshold: Single float for exact/gte/lte, or (low, high) tuple for range.

    Returns:
        True if the comparison is satisfied (→ Yes), False otherwise (→ No).

    Raises:
        ValueError: If the operator is unknown.
    """
    if operator == "exact":
        return value == threshold

    if operator == "gte":
        return value >= float(threshold)  # type: ignore[arg-type]

    if operator == "lte":
        return value <= float(threshold)  # type: ignore[arg-type]

    if operator == "range":
        low, high = threshold  # type: ignore[misc]
        return float(low) <= value <= float(high)  # type: ignore[arg-type]

    raise ValueError(f"Unknown operator: {operator}")
```

### 9.7 `src/reconciliation/__init__.py`

```python
"""Reconciliation stage — applies three gates and returns a verdict.

Entry point: reconcile(normalized, market_case, spec) → ReconciliationVerdict
"""

from __future__ import annotations

import logging

from src.normalization.models import NormalizedObservation
from src.reconciliation.finality import check_finality
from src.reconciliation.quality_gate import check_quality
from src.reconciliation.rule_parser import parse_rules
from src.reconciliation.comparator import compare
from src.reconciliation.models import ReconciliationVerdict
from src.retrieval.dispatch import RawObservationBatch
from src.retrieval.spec import RetrievalSpec
from src.validation.models import MarketCase

logger = logging.getLogger(__name__)


def reconcile(
    normalized: NormalizedObservation,
    market_case: MarketCase,
    spec: RetrievalSpec,
) -> ReconciliationVerdict:
    """Reconcile normalized observations against market rules.

    Three sequential gates:
    1. Finality — is the next-day datapoint published?
    2. Quality — are observations trustworthy?
    3. Comparison — does value meet threshold?

    Any gate can short-circuit to p4 or unclear.

    Args:
        normalized: NormalizedObservation from Stage 4.
        market_case: Original MarketCase (for title parsing).
        spec: RetrievalSpec (for finality from batch).

    Returns:
        ReconciliationVerdict with the final verdict.

    Note: The raw_batch is not directly passed — finality status should
    be carried through. For simplicity, we re-derive from spec context.
    In a full implementation, the batch's finality field is accessed
    via the PipelineContext (Stage 3's output).
    """
    # This function is designed to be called with batch info from the
    # PipelineContext. The runner's adapter extracts it before calling.
    pass  # See full implementation below
```

**In the actual implementation,** `reconcile()` needs the `RawObservationBatch` for finality. The `_call_stage` adapter in `runner.py` should pass it through. Update the adapter to pass `batch`:

```python
# In _call_stage in runner.py, the reconcile block:
if fn_name in ("reconcile",):
    if ctx.normalized is None or ctx.raw_batch is None:
        return ctx.replace(terminal=True, terminal_reason="unclear",
                          terminal_error=ValueError("Missing normalized or raw_batch"))
    verdict = raw_fn(ctx.normalized, ctx.raw_batch, ctx.case, ctx.spec)
    return ctx.replace(verdict=verdict)
```

Then `reconcile()` is:

```python
def reconcile(
    normalized: NormalizedObservation,
    batch: RawObservationBatch,
    market_case: MarketCase,
    spec: RetrievalSpec,
) -> ReconciliationVerdict:
    # ── Gate 1: Finality ──
    finality_verdict = check_finality(batch)
    if finality_verdict is not None:
        logger.info("Reconciliation blocked at finality gate: %s", finality_verdict.verdict)
        return finality_verdict

    # ── Gate 2: Quality ──
    quality_result = check_quality(normalized)
    if quality_result is not None and quality_result.verdict == "unclear":
        logger.info("Reconciliation blocked at quality gate: unclear")
        return quality_result
    penalties = quality_result.confidence_penalties if quality_result else ()

    # ── Gate 3: Parse rules & compare ──
    title = market_case.question_data.title
    parsed = parse_rules(title)

    if not parsed["parsed"]:
        return ReconciliationVerdict(
            verdict="unclear",
            gate="comparison",
            reasoning=f"Could not parse market rules from title: '{title}'",
        )

    operator = str(parsed["operator"])
    threshold = parsed["threshold"]
    value = normalized.value

    try:
        result = compare(value, operator, threshold)
    except ValueError as e:
        return ReconciliationVerdict(
            verdict="unclear",
            gate="comparison",
            reasoning=f"Comparison error: {e}",
        )

    # ── Build reasoning ──
    thr_str = (
        f"{threshold[0]}-{threshold[1]}"
        if isinstance(threshold, tuple)
        else str(threshold)
    )
    reasoning = (
        f"Station {spec.station_code} recorded a {spec.aggregation} of "
        f"{value}{normalized.unit} on {spec.target_window.start.date()}. "
        f"Market asked: {spec.measurement} {operator} {thr_str}{normalized.unit}? "
        f"{value} {'meets' if result else 'does not meet'} threshold → "
        f"{'Yes' if result else 'No'}."
    )

    verdict = "Yes" if result else "No"

    return ReconciliationVerdict(
        verdict=verdict,
        gate="comparison",
        operator=operator,
        threshold=threshold,
        comparison_result=result,
        confidence_penalties=penalties,
        reasoning=reasoning,
        normalized_value=value,
    )
```

---

## 10. Implement: Stage 6 — Decision

### 10.1 Package: `src/decision/`

| File | Purpose |
|---|---|
| `__init__.py` | (empty) |
| `resolver.py` | `resolve()` — deterministic mapping + conditional LLM review |
| `confidence.py` | Confidence calculation from objective factors |
| `reviewer.py` | LLM reviewer (safety valve — can only escalate to unclear) |
| `models.py` | `Resolution` dataclass |

### 10.2 `src/decision/models.py`

```python
"""Data models for decision output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class LLMReview:
    """Result of the LLM reviewer safety check.

    Attributes:
        invoked: Whether the reviewer was invoked.
        model: Which model was used.
        agreed: Whether the reviewer agreed with the deterministic result.
        reasoning: Reviewer's explanation.
    """

    invoked: bool = False
    model: str = ""
    agreed: bool = True
    reasoning: str = ""


@dataclass(frozen=True)
class Resolution:
    """Final resolution for a market case.

    Attributes:
        recommendation: p1, p2, p3, p4, or "unclear".
        confidence: 0.0 to 1.0, computed from objective factors.
        path: How the resolution was reached
            ("deterministic", "deterministic+llm_reviewed", "llm_escalated_to_unclear").
        llm_review: LLM review details (null if not invoked).
        reasoning: Concise explanation carried forward from reconciliation.
        review_reason: Why unclear or escalated (if applicable), else None.
    """

    recommendation: str  # "p1" | "p2" | "p3" | "p4" | "unclear"
    confidence: float
    path: str = "deterministic"
    llm_review: Optional[LLMReview] = None
    reasoning: str = ""
    review_reason: Optional[str] = None

    def __post_init__(self):
        valid = {"p1", "p2", "p3", "p4", "unclear"}
        if self.recommendation not in valid:
            raise ValueError(f"recommendation must be one of {valid}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be 0.0-1.0, got {self.confidence}")
```

### 10.3 `src/decision/confidence.py`

```python
"""Confidence calculation — objective, auditable factors, no LLM."""

from __future__ import annotations

from src.normalization.models import NormalizedObservation, QualityFlag


def compute_confidence(
    normalized: NormalizedObservation,
    source_path: str,  # "api", "playwright", "replay"
    confidence_penalties: tuple[tuple[str, float], ...] = (),
) -> float:
    """Compute confidence from objective factors.

    Args:
        normalized: Normalized observation (for completeness, quality_flags).
        source_path: How data was retrieved ("api", "playwright", "replay").
        confidence_penalties: Penalties from reconciliation's quality gate.

    Returns:
        Confidence score 0.0 – 1.0 (clamped to [0.50, 1.0] floor).
    """
    # Base confidence by retrieval path
    base_map = {
        "api": 0.95,
        "playwright": 0.85,
        "replay": 0.90,
    }
    confidence = base_map.get(source_path, 0.85)

    # Apply penalties from reconciliation
    for _flag_name, penalty in confidence_penalties:
        confidence -= penalty

    # Additional penalties from normalization quality flags
    for flag in normalized.quality_flags:
        confidence -= 0.08

    # Completeness penalty
    if normalized.completeness < 0.75:
        confidence -= 0.10

    # Floor at 0.50
    return max(0.50, min(1.0, confidence))
```

### 10.4 `src/decision/reviewer.py`

```python
"""LLM Reviewer — safety valve that can only escalate to unclear."""

from __future__ import annotations

import json
import logging

from src.decision.models import LLMReview
from src.observability.llm import get_llm_client

logger = logging.getLogger(__name__)

_REVIEWER_FALLBACK_PROMPT = """Review this weather market resolution for errors.

Market: {title}
Station: {station} ({station_code})
Measurement: {measurement} {aggregation} over {window}
Normalized value: {value}{unit} (expected precision: {precision}, completeness: {completeness})
Quality flags: {quality_flags}
Deterministic recommendation: {recommendation} (confidence: {confidence})
Reasoning: {reasoning}

Do you see any errors in the evidence chain, logical flaw in the comparison,
or reason this market should be unclear instead?

Answer ONLY with a JSON object: {{"agree": true, "reasoning": "..."}} or
{{"agree": false, "reasoning": "specific error found"}}"""


def review(
    recommendation: str,
    confidence: float,
    reasoning: str,
    title: str,
    station_code: str,
    station_url: str,
    measurement: str,
    aggregation: str,
    window: str,
    value: float,
    unit: str,
    precision: int,
    quality_flags: list[str],
    completeness: float,
) -> LLMReview:
    """Invoke the LLM reviewer to sanity-check a borderline resolution.

    The LLM can ONLY escalate to unclear. It cannot change p1→p2 or vice versa.
    It cannot increase confidence.

    Args:
        (all fields needed to present the full evidence chain to the LLM)

    Returns:
        LLMReview with invoked=True, agreed=True/False, and reasoning.
    """
    client = get_llm_client()

    # ── Fetch prompt from Langfuse, or use fallback ──
    try:
        prompt = client.get_prompt("weather-reviewer", label="production")
        compiled = prompt.compile(
            title=title,
            station=station_url,
            station_code=station_code,
            measurement=measurement,
            aggregation=aggregation,
            window=window,
            value=value,
            unit=unit,
            precision=precision,
            completeness=f"{completeness:.2f}",
            quality_flags=", ".join(quality_flags) if quality_flags else "none",
            recommendation=recommendation,
            confidence=f"{confidence:.2f}",
            reasoning=reasoning,
        )
        langfuse_prompt = prompt
    except Exception:
        logger.warning("Langfuse reviewer prompt unavailable; using fallback.")
        compiled = _REVIEWER_FALLBACK_PROMPT.format(
            title=title, station=station_url, station_code=station_code,
            measurement=measurement, aggregation=aggregation, window=window,
            value=value, unit=unit, precision=precision,
            completeness=f"{completeness:.2f}",
            quality_flags=", ".join(quality_flags) if quality_flags else "none",
            recommendation=recommendation, confidence=f"{confidence:.2f}",
            reasoning=reasoning,
        )
        langfuse_prompt = None

    response = client.complete(
        messages=[{"role": "user", "content": compiled}],
        temperature=0.0,
        max_tokens=512,
        langfuse_prompt=langfuse_prompt,
    )

    # ── Parse JSON response ──
    raw = response["content"].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # If we can't parse, default to agree (don't escalate on parse failure)
        logger.warning("LLM reviewer returned unparseable output: %s", raw[:200])
        return LLMReview(invoked=True, model=response["model"], agreed=True,
                        reasoning="Reviewer output unparseable; defaulting to agree.")

    agreed = parsed.get("agree", True)
    reviewer_reasoning = parsed.get("reasoning", "")

    return LLMReview(
        invoked=True,
        model=response["model"],
        agreed=bool(agreed),
        reasoning=reviewer_reasoning,
    )
```

### 10.5 `src/decision/resolver.py`

```python
"""Decision stage — maps reconciliation verdict to final recommendation.

Entry point: resolve(verdict, batch, normalized, market_case, spec) → Resolution
"""

from __future__ import annotations

import logging

from src.decision.confidence import compute_confidence
from src.decision.reviewer import review as llm_review
from src.decision.models import Resolution, LLMReview
from src.reconciliation.models import ReconciliationVerdict
from src.normalization.models import NormalizedObservation
from src.retrieval.dispatch import RawObservationBatch
from src.retrieval.spec import RetrievalSpec
from src.validation.models import MarketCase

logger = logging.getLogger(__name__)


def resolve(
    verdict: ReconciliationVerdict,
    batch: RawObservationBatch,
    normalized: NormalizedObservation,
    market_case: MarketCase,
    spec: RetrievalSpec,
) -> Resolution:
    """Produce the final resolution from a reconciliation verdict.

    1. Map verdict → recommendation (deterministic)
    2. Compute confidence from objective factors
    3. Conditionally invoke LLM reviewer (confidence < 0.85)
    4. Apply reviewer escalation (can only go to unclear)

    Args:
        verdict: ReconciliationVerdict from Stage 5.
        batch: RawObservationBatch (for source path info).
        normalized: NormalizedObservation (for quality, completeness).
        market_case: Original MarketCase (for title, case_id).
        spec: RetrievalSpec (for station, measurement context).

    Returns:
        Resolution with recommendation, confidence, path, and reasoning.
    """
    # ── Step 1: Deterministic mapping ──
    _VERDICT_MAP = {
        "Yes": "p2",
        "No": "p1",
        "p3_tie": "p3",
        "p4_too_early": "p4",
        "unclear": "unclear",
    }
    recommendation = _VERDICT_MAP.get(verdict.verdict, "unclear")

    # ── Step 2: Confidence ──
    # Determine source path from trace
    source_path = _extract_source_path(batch)
    confidence = compute_confidence(
        normalized=normalized,
        source_path=source_path,
        confidence_penalties=verdict.confidence_penalties,
    )

    # For unclear/p4 verdicts, confidence is 1.0 (we're certain it's unclear/too-early)
    if recommendation in ("unclear", "p4"):
        return Resolution(
            recommendation=recommendation,
            confidence=1.0,
            path="deterministic",
            reasoning=verdict.reasoning,
            review_reason=verdict.reasoning if recommendation == "unclear" else None,
        )

    # ── Step 3: LLM reviewer (conditional) ──
    llm_result: LLMReview | None = None
    path = "deterministic"

    if confidence < 0.85:
        logger.info("Confidence %.2f < 0.85 — invoking LLM reviewer.", confidence)
        llm_result = llm_review(
            recommendation=recommendation,
            confidence=confidence,
            reasoning=verdict.reasoning,
            title=market_case.question_data.title,
            station_code=spec.station_code,
            station_url=spec.station_url,
            measurement=spec.measurement,
            aggregation=spec.aggregation,
            window=f"{spec.target_window.start.date()} to {spec.target_window.end.date()}",
            value=normalized.value,
            unit=normalized.unit,
            precision=normalized.precision,
            quality_flags=[f.value for f in normalized.quality_flags],
            completeness=normalized.completeness,
        )
        path = "deterministic+llm_reviewed"

        if not llm_result.agreed:
            logger.warning(
                "LLM reviewer disagreed with recommendation %s — escalating to unclear.",
                recommendation,
            )
            return Resolution(
                recommendation="unclear",
                confidence=min(confidence, 0.70),
                path="llm_escalated_to_unclear",
                llm_review=llm_result,
                reasoning=verdict.reasoning,
                review_reason=(
                    f"LLM reviewer ({llm_result.model}) disagreed: {llm_result.reasoning}"
                ),
            )

        # For low-confidence cases (0.50-0.70), cap confidence even if reviewer agrees
        if confidence < 0.70:
            confidence = 0.70

    return Resolution(
        recommendation=recommendation,
        confidence=confidence,
        path=path,
        llm_review=llm_result,
        reasoning=verdict.reasoning,
    )


def _extract_source_path(batch: RawObservationBatch) -> str:
    """Determine the retrieval path from source trace entries."""
    for entry in batch.source_trace:
        if entry.error is None:
            return entry.path
    return "api"  # fallback
```

---

## 11. Implement: Stage 7 — Output Formatting

### 11.1 `src/output/__init__.py`

```python
"""Output formatting — assembles final JSON from Resolution + accumulated evidence."""
```

### 11.2 `src/output/formatter.py`

```python
"""Format results as structured JSON and write to disk."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.orchestration.runner import PipelineRun
from src.orchestration.context import PipelineContext

logger = logging.getLogger(__name__)


def format_results(run: PipelineRun, output_path: Path) -> None:
    """Format all pipeline results and write to output_path.

    Args:
        run: Completed PipelineRun with results for all cases.
        output_path: Path to write results JSON (e.g., output/results.json).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    results = [_format_single(ctx) for ctx in run.results]

    output = {
        "run_id": run.run_id,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat(),
        "total_cases": run.total_cases,
        "summary": run.summary,
        "results": results,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)

    logger.info("Wrote %d results to %s", len(results), output_path)


def _format_single(ctx: PipelineContext) -> dict:
    """Format a single PipelineContext into the output schema.

    Output shape per the case study spec:
        case_id, recommendation, confidence, evidence, source_trace,
        reasoning, review_reason
    """
    result: dict = {
        "case_id": ctx.case.case_id,
    }

    if ctx.resolution:
        res = ctx.resolution
        result.update({
            "recommendation": res.recommendation,
            "confidence": res.confidence,
            "decision_path": res.path,
            "reasoning": res.reasoning,
            "review_reason": res.review_reason,
        })
    else:
        # Terminal without resolution
        result.update({
            "recommendation": "unclear",
            "confidence": 0.0,
            "decision_path": "error",
            "reasoning": f"Pipeline terminated at stage '{ctx.stage}': {ctx.terminal_reason}",
            "review_reason": ctx.terminal_reason,
        })

    # ── Evidence ──
    evidence: dict = {}
    if ctx.spec:
        evidence["station"] = ctx.spec.station_url
        evidence["station_code"] = ctx.spec.station_code
        evidence["window"] = f"{ctx.spec.target_window.start.date()} to {ctx.spec.target_window.end.date()}"
        evidence["measurement"] = ctx.spec.measurement
        evidence["aggregation"] = ctx.spec.aggregation
        evidence["unit"] = ctx.spec.unit
        evidence["precision"] = ctx.spec.precision

    if ctx.normalized:
        evidence["observed_value"] = ctx.normalized.value
        evidence["observed_unit"] = ctx.normalized.unit
        evidence["observation_count"] = ctx.normalized.observation_count
        evidence["completeness"] = round(ctx.normalized.completeness, 2)

    result["evidence"] = evidence

    # ── Source trace ──
    source_trace: list[dict] = []
    if ctx.raw_batch:
        for entry in ctx.raw_batch.source_trace:
            source_trace.append({
                "url": entry.url,
                "http_status": entry.http_status,
                "latency_ms": entry.latency_ms,
                "path": entry.path,
                "retry_count": entry.retry_count,
                "guardrail_flags": entry.guardrail_flags,
                "error": entry.error,
                "timestamp": entry.timestamp,
            })
    result["source_trace"] = source_trace

    return result
```

---

## 12. Entry Points: `resolve.py` + `evaluate.py`

### 12.1 `resolve.py`

```python
#!/usr/bin/env python3
"""OTB Weather Market Resolver — entry point.

Usage:
    # Replay mode (deterministic, uses fixtures)
    python resolve.py --input data/markets.json --fixtures data/fixtures

    # Live mode (fetches from Wunderground, records snapshots)
    python resolve.py --input data/markets.json --fixtures data/fixtures --live

    # Run a single case
    python resolve.py --input data/markets.json --fixtures data/fixtures --live --case-id tokyo_low_2026_06_01_20c

    # Capture fixtures
    python resolve.py --capture-fixtures --input data/markets.json
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

# Load .env before any other imports that read env vars
load_dotenv()

from src.observability.logging import configure_logging
from src.observability.tracing import flush as flush_langfuse
from src.orchestration.runner import PipelineRunner

logger = logging.getLogger("otb-resolver")


def main():
    parser = argparse.ArgumentParser(
        description="OTB Weather Market Resolver",
    )
    parser.add_argument(
        "--input", required=True,
        help="Path to markets.json manifest.",
    )
    parser.add_argument(
        "--fixtures", default="data/fixtures",
        help="Directory for fixture files (default: data/fixtures).",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Live mode: fetch from Wunderground/NOAA instead of replay.",
    )
    parser.add_argument(
        "--capture-fixtures", action="store_true",
        help="Capture fixtures: record live responses into fixtures/.",
    )
    parser.add_argument(
        "--case-id",
        help="Run only the specified case_id (for debugging).",
    )
    parser.add_argument(
        "--output", default="output/results.json",
        help="Output path for results JSON (default: output/results.json).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO).",
    )
    parser.add_argument(
        "--pipeline-config", default="config/pipeline.yaml",
        help="Path to pipeline YAML config (default: config/pipeline.yaml).",
    )

    args = parser.parse_args()

    # Configure structured logging
    configure_logging(level=args.log_level)

    # Determine mode
    if args.live and args.capture_fixtures:
        print("Error: --live and --capture-fixtures are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    mode = "replay"
    if args.live:
        mode = "live"
    elif args.capture_fixtures:
        mode = "capture"

    logger.info("Starting OTB Weather Resolver (mode=%s)", mode)

    try:
        # Build and run pipeline
        runner = PipelineRunner.from_yaml(args.pipeline_config)
        run = runner.run(
            input_path=args.input,
            mode=mode,
            fixtures_dir=args.fixtures,
            case_id=args.case_id,
        )

        # Write results
        runner.write_results(run, args.output)

        # Print summary
        print(f"\n{'='*60}")
        print(f"Run: {run.run_id}")
        print(f"Cases: {run.total_cases}")
        print(f"Results: {run.summary}")
        print(f"Output: {args.output}")
        print(f"{'='*60}")

    except Exception as e:
        logger.error("Pipeline failed: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        # Flush Langfuse traces before exit
        flush_langfuse()


if __name__ == "__main__":
    main()
```

### 12.2 `evaluate.py`

```python
#!/usr/bin/env python3
"""Evaluation script — compares resolver output against gold answers.

Usage:
    python evaluate.py --predictions output/results.json --gold gold_visible/answers.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Evaluate resolver output against gold answers.")
    parser.add_argument("--predictions", required=True, help="Path to resolver output JSON.")
    parser.add_argument("--gold", required=True, help="Path to gold answers JSON.")
    args = parser.parse_args()

    with open(args.predictions, encoding="utf-8") as f:
        preds = json.load(f)

    with open(args.gold, encoding="utf-8") as f:
        gold = json.load(f)

    # Index gold by case_id
    gold_by_id = {g["case_id"]: g for g in gold.get("results", gold)}

    total = 0
    correct = 0
    false_confident = 0
    false_unclear = 0
    conservative_saves = 0  # unclear that would have been wrong

    for pred in preds.get("results", []):
        case_id = pred["case_id"]
        gold_case = gold_by_id.get(case_id)
        if gold_case is None:
            continue

        total += 1
        pred_rec = pred.get("recommendation", "unclear")
        gold_rec = gold_case.get("recommendation", "")
        confidence = pred.get("confidence", 0.0)

        if pred_rec == gold_rec:
            correct += 1
        elif pred_rec in ("unclear", "p3"):
            # Conservative: returned unclear instead of guessing
            conservative_saves += 1
        elif confidence >= 0.7:
            # Wrong and confident — worst failure mode
            false_confident += 1
        else:
            # Wrong but low confidence — less bad
            false_unclear += 1

    print(f"\n{'='*60}")
    print("Evaluation Results")
    print(f"{'='*60}")
    print(f"Total cases:           {total}")
    print(f"Correct:               {correct} ({_pct(correct, total)})")
    print(f"Conservative (unclear): {conservative_saves} ({_pct(conservative_saves, total)})")
    print(f"False confident (BAD):  {false_confident}")
    print(f"False unclear:          {false_unclear}")
    print(f"{'='*60}")


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0.0%"
    return f"{n / total * 100:.1f}%"


if __name__ == "__main__":
    main()
```

---

## 13. Prompt Management in Langfuse

### 13.1 Prompts to Create

Once Langfuse is running at `localhost:3000`, log in and create these prompts:

#### Prompt 1: `weather-spec-extraction`

**Type:** Chat
**Label:** `production`

**System message (optional):** *(leave empty)*

**User message:**
```
Extract these fields from the weather market resolution instructions below as a single JSON object. Return ONLY valid JSON, no markdown fences, no extra text.

Required fields:
- source_type: "wunderground_station" if wunderground.com, "noaa_monthly" if weather.gov or noaa.gov
- station_url: exact URL from the text
- station_code: 4-letter ICAO code from the URL (e.g. RJTT, KBKF, RKSI)
- target_window_start: date as YYYY-MM-DD
- target_window_end: same as start for single-day markets, as YYYY-MM-DD
- measurement: one of [temperature, precipitation, wind_speed, wind_gust, humidity, visibility, pressure, snow, uv_index, cloud_cover, dew_point]
- aggregation: "min" for lowest/minimum, "max" for highest/maximum, "sum" for total/precipitation, "point" for specific timestamp
- unit: "C" for Celsius, "F" for Fahrenheit, "in" for inches, "mm" for millimeters
- precision: integer decimal places. "whole degrees" = 1. "2 decimal places" = 2. "3 decimal places" = 3.
- timezone: IANA timezone like "Asia/Tokyo", "America/Denver", "Asia/Seoul", "Pacific/Auckland"
- finality_after: YYYY-MM-DD, day after window_end (markets cannot resolve until next-day data published)

Title: {{title}}

Ancillary data:
{{ancillary_data}}
```

#### Prompt 2: `weather-reviewer`

**Type:** Chat
**Label:** `production`

**User message:**
```
Review this weather market resolution for errors.

Market: {{title}}
Station: {{station}} ({{station_code}})
Measurement: {{measurement}} {{aggregation}} over {{window}}
Normalized value: {{value}}{{unit}} (expected precision: {{precision}}, completeness: {{completeness}})
Quality flags: {{quality_flags}}
Deterministic recommendation: {{recommendation}} (confidence: {{confidence}})
Reasoning: {{reasoning}}

Do you see any errors in the evidence chain, logical flaw in the comparison, or reason this market should be unclear instead?

Answer ONLY with a JSON object: {"agree": true, "reasoning": "..."} or {"agree": false, "reasoning": "specific error found"}
```

**Variables** (declared in Langfuse UI): `title`, `station`, `station_code`, `measurement`, `aggregation`, `window`, `value`, `unit`, `precision`, `completeness`, `quality_flags`, `recommendation`, `confidence`, `reasoning`

### 13.2 Langfuse API Keys for Code

The keys are in `.env`:
```
LANGFUSE_PUBLIC_KEY=pk-lf-otb-dev
LANGFUSE_SECRET_KEY=sk-lf-otb-dev
LANGFUSE_HOST=http://localhost:3000
```

Copy these from the Langfuse UI (Settings → API Keys) after the first boot.

---

## 14. Testing Strategy

### 14.1 Test Organization

```
tests/
├── conftest.py                  # Shared fixtures (already exists, extend as needed)
├── validation/                  # Already exists — tests for Stage 1
├── retrieval/                   # Already exists — tests for Stages 2-3
├── normalization/
│   ├── test_convert.py          # Unit conversion edge cases
│   ├── test_round.py            # Precision rounding
│   ├── test_quality.py          # Quality flag logic
│   ├── test_anomaly.py          # Physical limit checks
│   └── test_normalize.py        # End-to-end normalization
├── reconciliation/
│   ├── test_finality.py         # Finality gate
│   ├── test_quality_gate.py     # Quality gate
│   ├── test_rule_parser.py      # Regex patterns for all market types
│   └── test_comparator.py       # Comparison math
├── decision/
│   ├── test_confidence.py       # Confidence scoring
│   └── test_resolver.py         # Deterministic mapping + reviewer logic
├── orchestration/
│   ├── test_context.py          # PipelineContext immutability
│   └── test_runner.py           # Runner sequences stages correctly
└── integration/
    └── test_pipeline_e2e.py     # Full pipeline run with fixtures
```

### 14.2 Key Test Patterns

```python
# tests/normalization/test_normalize.py
def test_normalize_a_day_of_observations():
    """Full normalization of a clean single-day batch."""
    batch = _make_raw_batch(temp_values=[18, 19, 20, ...], unit="C")
    spec = _make_spec(unit="C", precision=1, measurement="temperature")
    result = normalize(batch, spec)
    assert result.value == 18.0  # min of observations
    assert result.unit == "C"
    assert result.completeness == 1.0
    assert result.is_clean

def test_normalize_unit_conversion():
    """F → C conversion during normalization."""
    batch = _make_raw_batch(temp_values=[68.0], unit="F")
    spec = _make_spec(unit="C", precision=1, measurement="temperature")
    result = normalize(batch, spec)
    assert result.value == 20.0  # (68-32)*5/9 = 20
    assert result.unit == "C"
    assert result.raw_value == 68.0
    assert result.raw_unit == "F"

def test_normalize_anomaly_gates():
    """Impossible values gate to error."""
    batch = _make_raw_batch(temp_values=[95.0], unit="C")  # 95°C in Tokyo?
    spec = _make_spec(unit="C", precision=1, measurement="temperature")
    with pytest.raises(NormalizationError, match="anomalous_data"):
        normalize(batch, spec)
```

### 14.3 Testing with Fixtures

The existing fixtures in `data/fixtures/` contain real captured observations. Unit tests should load these and run normalization/reconciliation/decision on them, asserting expected outputs.

See tests/retrieval/test_retrieval.py and tests/retrieval/test_spec.py for existing patterns — follow the same style.

---

## 15. Implementation Order (Checklist)

### Infrastructure (do first)

- [ ] 1. Create `docker-compose.yml` (copy from §2.2)
- [ ] 2. Create `litellm_config.yaml` (copy from §2.3)
- [ ] 3. Update `.env` with Langfuse + LiteLLM vars (§2.4)
- [ ] 4. `docker compose up -d` — verify both services are healthy
- [ ] 5. Log into Langfuse at `localhost:3000`, copy API keys
- [ ] 6. Create prompts in Langfuse UI (§13.1)
- [ ] 7. Update `pyproject.toml`: add `langfuse`, `openai`, `litellm`, `structlog`, `pyyaml`, `pytz`, `tenacity`, `python-dotenv`; remove `google-genai`

### Orchestration + Observability

- [ ] 8. Create `src/orchestration/__init__.py`
- [ ] 9. Create `src/orchestration/context.py` (§3.3)
- [ ] 10. Create `src/orchestration/steps.py` (§3.4)
- [ ] 11. Create `src/orchestration/runner.py` (§3.5)
- [ ] 12. Create `src/orchestration/config.py` (§3.6)
- [ ] 13. Create `src/observability/__init__.py`
- [ ] 14. Create `src/observability/tracing.py` (§4.2)
- [ ] 15. Create `src/observability/logging.py` (§4.3)
- [ ] 16. Create `src/observability/llm.py` (§4.4)

### Refactor Existing Stages

- [ ] 17. Rewrite `src/retrieval/llm_extractor.py` (§7.2) — switch to LiteLLM + Langfuse prompts
- [ ] 18. Update `compose_retrieval_spec` to use `create_litellm_extractor` by default
- [ ] 19. Remove old `create_gemini_extractor` function
- [ ] 20. Verify Stage 2 works with LiteLLM proxy

### New Stages

- [ ] 21. Create `src/normalization/models.py` (§8.2)
- [ ] 22. Create `src/normalization/convert.py` (§8.3)
- [ ] 23. Create `src/normalization/round.py` (§8.4)
- [ ] 24. Create `src/normalization/quality.py` (§8.5)
- [ ] 25. Create `src/normalization/anomaly.py` (§8.6)
- [ ] 26. Create `src/normalization/verify.py` (§8.7)
- [ ] 27. Create `src/normalization/__init__.py` (§8.8)
- [ ] 28. Create `src/reconciliation/models.py` (§9.2)
- [ ] 29. Create `src/reconciliation/finality.py` (§9.3)
- [ ] 30. Create `src/reconciliation/quality_gate.py` (§9.4)
- [ ] 31. Create `src/reconciliation/rule_parser.py` (§9.5)
- [ ] 32. Create `src/reconciliation/comparator.py` (§9.6)
- [ ] 33. Create `src/reconciliation/__init__.py` (§9.7)
- [ ] 34. Create `src/decision/models.py` (§10.2)
- [ ] 35. Create `src/decision/confidence.py` (§10.3)
- [ ] 36. Create `src/decision/reviewer.py` (§10.4)
- [ ] 37. Create `src/decision/resolver.py` (§10.5)
- [ ] 38. Create `src/output/__init__.py`
- [ ] 39. Create `src/output/formatter.py` (§11.2)

### Configuration + Entry Points

- [ ] 40. Create `config/pipeline.yaml` (§6)
- [ ] 41. Create `resolve.py` (§12.1)
- [ ] 42. Create `evaluate.py` (§12.2)
- [ ] 43. Create `gold_visible/answers.json` with expected answers for the 5 visible cases

### Tests

- [ ] 44. Unit tests for normalization (convert, round, quality, anomaly)
- [ ] 45. Unit tests for reconciliation (finality, quality gate, rule parser, comparator)
- [ ] 46. Unit tests for decision (confidence computation, verdict mapping)
- [ ] 47. Integration test: full pipeline with `--mode replay` against fixtures
- [ ] 48. Run `python resolve.py --input data/markets.json --fixtures data/fixtures` and verify output

---

## 16. Expected Final Project Structure

```
.
├── AGENTS.md
├── IMPLEMENTATION.md          # This file
├── README.md
├── DESIGN.md
├── resolve.py                 # Entry point
├── evaluate.py                # Evaluation script
├── requirements.txt           # (if using pip) or pyproject.toml
├── pyproject.toml             # Updated with new deps
├── .env                       # Updated with Langfuse + LiteLLM vars
├── docker-compose.yml         # Langfuse + LiteLLM stack
├── litellm_config.yaml        # Model routing config
├── config/
│   └── pipeline.yaml          # Pipeline stage definitions
├── data/
│   ├── markets.json
│   ├── fixtures/
│   │   ├── RJTT_20260601_temperature_min.json
│   │   ├── KBKF_20260531_temperature_max.json
│   │   └── KSEA_20260601_precipitation_sum.json
│   └── schema/
│       └── market_input.schema.json
├── gold_visible/
│   └── answers.json
├── output/
│   └── (results.json written here)
├── src/
│   ├── __init__.py
│   ├── validation/            # Stage 1 (existing, complete)
│   │   ├── __init__.py
│   │   ├── loader.py
│   │   ├── models.py
│   │   └── schema.py
│   ├── retrieval/             # Stages 2-3 (existing, refactored)
│   │   ├── __init__.py
│   │   ├── spec.py
│   │   ├── models.py
│   │   ├── dispatch.py
│   │   ├── llm_extractor.py   # REWRITTEN — uses LiteLLM + Langfuse
│   │   ├── regex_fallback.py
│   │   ├── station_registry.py
│   │   ├── guardrails.py
│   │   ├── wunderground_api.py
│   │   ├── wunderground_playwright.py
│   │   ├── noaa.py
│   │   └── replay.py
│   ├── normalization/         # Stage 4 (NEW)
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── convert.py
│   │   ├── round.py
│   │   ├── verify.py
│   │   ├── quality.py
│   │   └── anomaly.py
│   ├── reconciliation/        # Stage 5 (NEW)
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── finality.py
│   │   ├── quality_gate.py
│   │   ├── rule_parser.py
│   │   └── comparator.py
│   ├── decision/              # Stage 6 (NEW)
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── confidence.py
│   │   ├── reviewer.py
│   │   └── resolver.py
│   ├── output/                # Stage 7 (NEW)
│   │   ├── __init__.py
│   │   └── formatter.py
│   ├── orchestration/         # NEW — thin orchestration layer
│   │   ├── __init__.py
│   │   ├── context.py
│   │   ├── steps.py
│   │   ├── runner.py
│   │   └── config.py
│   └── observability/         # NEW — Langfuse + logging
│       ├── __init__.py
│       ├── tracing.py
│       ├── logging.py
│       └── llm.py
└── tests/
    ├── conftest.py
    ├── validation/            # (existing)
    ├── retrieval/             # (existing)
    ├── normalization/         # (NEW)
    ├── reconciliation/        # (NEW)
    ├── decision/              # (NEW)
    ├── orchestration/         # (NEW)
    └── integration/           # (NEW)
```

---

## Appendix A: Key Design Decisions

1. **LiteLLM as a proxy (not library).** The proxy runs as a Docker sidecar. Application code talks to it via OpenAI-compatible API. This decouples provider configuration from application code — adding a new model requires only a `litellm_config.yaml` change + restart.

2. **Langfuse prompts are fetched at runtime, not baked in.** The `LLMClient.get_prompt()` method fetches the current `production` label from Langfuse. If Langfuse is down, the fallback hardcoded prompt is used. This means prompts can be iterated without code changes.

3. **One-way LLM reviewer.** The reviewer can only escalate to `unclear` — it cannot override a correct deterministic result, change p1→p2, or increase confidence. This preserves the conservative bias.

4. **Normalization is 100% deterministic.** No LLM involvement. Unit conversion, rounding, quality checks, and anomaly detection are pure math and threshold comparisons.

5. **Reconciliation rule parsing is regex, not LLM.** Polymarket weather titles follow constrained patterns. Regex covers all known variants cleanly and deterministically.

6. **Immutable pipeline context.** `PipelineContext` is a frozen dataclass. Stages return new instances via `replace()`. This makes debugging trivial — you can serialize the context at any point.

7. **Terminal gating, not exception propagation.** When a stage determines a case is `unclear` or `p4`, it sets `ctx.terminal=True` rather than raising. The runner checks this flag and stops processing that case. This is cleaner than try/except chains.

## Appendix B: Quick Start (After Implementation)

```bash
# 1. Start infrastructure
docker compose up -d

# 2. Install dependencies
pip install -e ".[dev]"

# 3. Run in replay mode
python resolve.py --input data/markets.json --fixtures data/fixtures

# 4. Run in live mode (fetches from Wunderground)
python resolve.py --input data/markets.json --fixtures data/fixtures --live

# 5. Run a single case
python resolve.py --input data/markets.json --fixtures data/fixtures --live --case-id tokyo_low_2026_06_01_20c

# 6. Evaluate against gold answers
python evaluate.py --predictions output/results.json --gold gold_visible/answers.json

# 7. Run tests
pytest tests/ -v
```
