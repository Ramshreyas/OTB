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
_FALLBACK_PROMPT = """Extract these fields from the weather market resolution instructions below as a single JSON object. Return ONLY valid JSON, no markdown fences, no extra text.

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
        """Extract RetrievalSpec fields using LLM via LiteLLM proxy.

        Retries up to 3 times with exponential backoff on transient failures.
        """
        import time
        last_error = None
        for attempt in range(3):
            try:
                # ── Fetch prompt from Langfuse, or use fallback ──
                try:
                    prompt = client.get_prompt("weather-spec-extraction", label="production")
                    compiled = prompt.compile(title=title, ancillary_data=ancillary_data)
                    langfuse_prompt = prompt
                except Exception:
                    logger.warning("Langfuse prompt unavailable; using fallback prompt.")
                    compiled = _FALLBACK_PROMPT.format(title=title, ancillary_data=ancillary_data)
                    langfuse_prompt = None

                logger.info("Calling LLM (%s) for spec extraction (attempt %d/3)...",
                           client.model, attempt + 1)

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
                llm_result = json.loads(raw_text)
                return _normalize_result(llm_result)

            except json.JSONDecodeError as e:
                logger.error("LLM returned invalid JSON (attempt %d/3): %s", attempt + 1, e)
                last_error = ValueError(f"LLM returned invalid JSON: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
            except Exception as e:
                logger.warning("LLM extraction attempt %d/3 failed: %s", attempt + 1, e)
                last_error = e
                if attempt < 2:
                    time.sleep(2 ** attempt)

        raise last_error  # type: ignore[misc]

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
