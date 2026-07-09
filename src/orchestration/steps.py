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
    """Wrap the function call in a Langfuse span if Langfuse is configured.

    Uses Langfuse v3 API: start_as_current_observation(as_type="span", ...)
    Nested spans are created automatically via OpenTelemetry context propagation.

    Each stage captures rich input (what it receives from prior stages) and
    output (what it produces), so the Langfuse trace is fully self-documenting.
    """
    try:
        from src.observability.tracing import get_langfuse_client
        client = get_langfuse_client()
        if client is not None:
            span_input = _build_stage_input(name, ctx, deps)

            with client.start_as_current_observation(
                name=f"stage/{name}",
                as_type="span",
                input=span_input,
            ) as span_ctx:
                result = fn(ctx, **deps)
                output_meta = _build_stage_output(name, result)

                # Update the span via Langfuse v3 API
                try:
                    from langfuse import get_client as _get_client
                    _lc = _get_client()
                    if _lc:
                        _lc.update_current_span(output=output_meta)
                        if result.terminal:
                            _lc.update_current_span(
                                level="WARNING",
                                status_message=result.terminal_reason,
                            )
                except Exception:
                    pass
                return result
    except (ImportError, Exception) as e:
        logger.debug("Langfuse tracing unavailable: %s", e)
    return fn(ctx, **deps)


def _build_stage_input(name: str, ctx: PipelineContext, deps: dict[str, Any]) -> dict:
    """Build the input dict for a stage span based on what's available in context."""
    inp: dict = {
        "case_id": ctx.case.case_id,
        "previous_stage": ctx.stage,
    }

    if name == "compose_spec":
        # ── Stage 2: composing retrieval spec from market case ──
        inp["title"] = ctx.case.question_data.title
        # ancillary_data can be large; truncate to keep spans readable
        anc = ctx.case.ancillary_data or ""
        inp["ancillary_data"] = anc[:800] + ("..." if len(anc) > 800 else "")
        inp["proposal_time"] = str(ctx.case.question_data.proposal_time)
        inp["mode"] = deps.get("mode", "unknown")

    elif name == "retrieve":
        # ── Stage 3: fetching weather data ──
        if ctx.spec:
            inp["station_code"] = ctx.spec.station_code
            inp["station_url"] = ctx.spec.station_url
            inp["source_type"] = ctx.spec.source_type
            inp["measurement"] = ctx.spec.measurement
            inp["aggregation"] = ctx.spec.aggregation
            inp["unit"] = ctx.spec.unit
            inp["window_start"] = str(ctx.spec.target_window.start.date())
            inp["window_end"] = str(ctx.spec.target_window.end.date())
            inp["timezone"] = ctx.spec.timezone
            inp["finality_after"] = str(ctx.spec.finality_after) if ctx.spec.finality_after else None
        inp["mode"] = deps.get("mode", "unknown")

    elif name == "normalize":
        # ── Stage 4: normalizing raw observations ──
        if ctx.spec:
            inp["target_unit"] = ctx.spec.unit
            inp["target_precision"] = ctx.spec.precision
            inp["measurement"] = ctx.spec.measurement
            inp["aggregation"] = ctx.spec.aggregation
        if ctx.raw_batch:
            inp["raw_observation_count"] = len(ctx.raw_batch.observations)
            inp["finality_status"] = str(ctx.raw_batch.finality)
            # Show a sample of raw observations (first 3) — handles both dict and object formats
            sample = []
            for obs in ctx.raw_batch.observations[:3]:
                if isinstance(obs, dict):
                    sample.append({
                        "ts": obs.get("valid_time_gmt", "?"),
                        "temp": obs.get("temp", obs.get("value", "?")),
                    })
                else:
                    sample.append({
                        "ts": str(getattr(obs, "valid_time_gmt", getattr(obs, "timestamp", "?"))),
                        "value": getattr(obs, "temp", getattr(obs, "value", "?")),
                    })
            inp["raw_sample"] = sample

    elif name == "reconcile":
        # ── Stage 5: reconciling normalized value against market rules ──
        if ctx.normalized:
            inp["normalized_value"] = ctx.normalized.value
            inp["normalized_unit"] = ctx.normalized.unit
            inp["completeness"] = ctx.normalized.completeness
            inp["quality_flags"] = [f.value if hasattr(f, 'value') else str(f)
                                    for f in ctx.normalized.quality_flags]
        if ctx.spec:
            inp["measurement"] = ctx.spec.measurement
            inp["aggregation"] = ctx.spec.aggregation

    elif name == "decide":
        # ── Stage 6: final decision ──
        if ctx.verdict:
            inp["verdict"] = ctx.verdict.verdict
            inp["gate"] = ctx.verdict.gate
            inp["comparison_result"] = ctx.verdict.comparison_result
            inp["confidence_penalties"] = [
                {"reason": p.reason, "amount": p.amount}
                if hasattr(p, 'reason') else str(p)
                for p in ctx.verdict.confidence_penalties
            ]
            inp["reconciliation_reasoning"] = ctx.verdict.reasoning[:500] if ctx.verdict.reasoning else ""
        if ctx.normalized:
            inp["observed_value"] = ctx.normalized.value
            inp["observed_unit"] = ctx.normalized.unit

    return inp


def _build_stage_output(name: str, result: PipelineContext) -> dict:
    """Build the output dict for a stage span from the result context."""
    out: dict = {
        "terminal": result.terminal,
        "stage": name,
    }

    if name == "compose_spec" and result.spec:
        s = result.spec
        out.update({
            "source_type": s.source_type,
            "station_code": s.station_code,
            "station_url": s.station_url,
            "measurement": s.measurement,
            "aggregation": s.aggregation,
            "unit": s.unit,
            "precision": s.precision,
            "timezone": s.timezone,
            "window_start": str(s.target_window.start.date()),
            "window_end": str(s.target_window.end.date()),
            "finality_after": str(s.finality_after) if s.finality_after else None,
            "extraction_method": s.extraction_method,
            "cross_validation": str(s.cross_validation),
        })

    elif name == "retrieve" and result.raw_batch:
        b = result.raw_batch
        out["observation_count"] = len(b.observations)
        out["finality"] = str(b.finality)
        # Full source trace entries
        out["source_trace"] = [
            {
                "url": e.url,
                "http_status": e.http_status,
                "latency_ms": e.latency_ms,
                "path": e.path,
                "retry_count": e.retry_count,
                "guardrail_flags": e.guardrail_flags,
                "error": e.error,
                "timestamp": str(e.timestamp),
            }
            for e in b.source_trace
        ]

    elif name == "normalize" and result.normalized:
        n = result.normalized
        out.update({
            "value": n.value,
            "unit": n.unit,
            "precision": n.precision,
            "raw_value": n.raw_value,
            "raw_unit": n.raw_unit,
            "observation_count": n.observation_count,
            "expected_count": n.expected_count,
            "completeness": round(n.completeness, 3),
            "quality_flags": [f.value if hasattr(f, 'value') else str(f)
                              for f in n.quality_flags],
            "anomaly_flags": [f.value if hasattr(f, 'value') else str(f)
                              for f in n.anomaly_flags],
        })

    elif name == "reconcile" and result.verdict:
        v = result.verdict
        out.update({
            "verdict": v.verdict,
            "gate": v.gate,
            "operator": v.operator,
            "threshold": v.threshold,
            "comparison_result": v.comparison_result,
            "normalized_value": v.normalized_value,
            "reasoning": v.reasoning[:800] if v.reasoning else "",
            "confidence_penalties": [
                {"reason": p.reason, "amount": p.amount}
                if hasattr(p, 'reason') else str(p)
                for p in v.confidence_penalties
            ],
        })

    elif name == "decide" and result.resolution:
        r = result.resolution
        out.update({
            "recommendation": r.recommendation,
            "confidence": r.confidence,
            "path": r.path,
            "reasoning": r.reasoning[:500] if r.reasoning else "",
        })
        if r.review_reason:
            out["review_reason"] = r.review_reason
        if r.llm_review:
            out["llm_reviewer_invoked"] = r.llm_review.invoked
            out["llm_reviewer_agreed"] = r.llm_review.agreed

    if result.terminal_reason:
        out["terminal_reason"] = result.terminal_reason

    return out


def _case_id(ctx: PipelineContext) -> str:
    """Extract case_id from context for logging."""
    try:
        return ctx.case.case_id
    except Exception:
        return "?"
