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
        # compiled is a list of messages from a chat prompt.
        # Use directly as messages (don't double-wrap).
        if isinstance(compiled, list):
            messages = compiled
        else:
            messages = [{"role": "user", "content": compiled}]
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
        messages = [{"role": "user", "content": compiled}]
        langfuse_prompt = None

    try:
        response = client.complete(
            messages=messages,
            temperature=0.0,
            max_tokens=512,
            langfuse_prompt=langfuse_prompt,
            generation_name="reviewer-check",
        )
    except Exception as e:
        logger.warning("LLM reviewer call failed (connection or other error): %s. "
                       "Defaulting to agree with deterministic result.", e)
        return LLMReview(
            invoked=True,
            model=client.model,
            agreed=True,
            reasoning=f"Reviewer unavailable: {e}. Defaulting to agree with deterministic result.",
        )

    # ── Parse JSON response ──
    raw = response["content"].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
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
