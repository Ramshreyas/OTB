"""PipelineRunner — sequences stages through PipelineContext for each market case."""

from __future__ import annotations

import importlib
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
        run_id: Unique identifier for this run (timestamp-based).
        started_at: UTC timestamp when the run started.
        completed_at: UTC timestamp when the run finished.
        total_cases: Number of market cases in the manifest.
        results: List of PipelineContext objects (one per case, in order).
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
            **extra_kwargs: Passed through to stage functions.

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
    module_path = stage_def["module"]
    function_name = stage_def["function"]
    on_error = stage_def.get("on_error", "raise")

    mod = importlib.import_module(module_path)
    raw_fn = getattr(mod, function_name)

    from src.orchestration.steps import step
    stage_name = stage_def.get("name", function_name)
    stage_num = stage_def.get("stage_num", 0)

    @step(name=stage_name, stage_num=stage_num, on_error=on_error)
    def _wrapped(ctx: PipelineContext, **kwargs: Any) -> PipelineContext:
        """Adapt raw stage function to PipelineContext protocol."""
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
    """
    fn_name = raw_fn.__name__

    # ── Stage 2: compose_retrieval_spec ──
    if fn_name in ("compose_retrieval_spec",):
        # Only pass through args compose_retrieval_spec accepts
        spec_kwargs = {k: v for k, v in kwargs.items()
                       if k in ("llm_extractor", "use_litellm")}
        spec = raw_fn(ctx.case, **spec_kwargs)
        return ctx.replace(spec=spec)

    # ── Stage 3: retrieve_observations ──
    if fn_name in ("retrieve_observations",):
        if ctx.spec is None:
            return ctx.replace(terminal=True, terminal_reason="unclear",
                              terminal_error=ValueError("No RetrievalSpec available"))
        # Pass mode, fixtures_dir, fixture_path_override
        retrieve_kwargs = {k: v for k, v in kwargs.items()
                          if k in ("mode", "fixtures_dir", "fixture_path_override", "api_key")}
        batch = raw_fn(ctx.spec, **retrieve_kwargs)
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
        if ctx.normalized is None or ctx.raw_batch is None:
            return ctx.replace(terminal=True, terminal_reason="unclear",
                              terminal_error=ValueError("Missing normalized or raw_batch"))
        verdict = raw_fn(ctx.normalized, ctx.raw_batch, ctx.case, ctx.spec)
        return ctx.replace(verdict=verdict)

    # ── Stage 6: decide / resolve ──
    if fn_name in ("resolve", "make_decision"):
        if ctx.verdict is None or ctx.normalized is None or ctx.raw_batch is None:
            return ctx.replace(terminal=True, terminal_reason="unclear",
                              terminal_error=ValueError("Missing verdict, normalized, or raw_batch"))
        resolution = raw_fn(ctx.verdict, ctx.raw_batch, ctx.normalized, ctx.case, ctx.spec)
        return ctx.replace(resolution=resolution)

    # ── Stage 7: format_output — handled by write_results() ──

    raise ValueError(f"Unknown stage function: {fn_name}")
