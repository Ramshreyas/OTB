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
