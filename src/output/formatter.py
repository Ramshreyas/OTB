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
