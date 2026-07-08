"""LLM Extractor — Uses Gemini to extract RetrievalSpec fields from ancillary_data.

This is the primary extraction path. The LLM receives the full ancillary_data
string and question title, and returns structured JSON with all fields.

The prompt is concise to avoid truncation and uses explicit precision mapping
("whole degrees" → 1).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Short, direct prompt — avoids verbose instructions that confuse the model
_EXTRACTION_PROMPT = """Extract these fields from the weather market resolution instructions below as a single JSON object. Return ONLY valid JSON, no markdown fences, no extra text.

- source_type: "wunderground_station" if URL is wunderground.com, "noaa_monthly" if weather.gov or noaa.gov
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

Title: {title}

Ancillary data:
{ancillary_data}"""


def create_gemini_extractor(model_name: str = "gemini-2.5-flash"):
    """Create an LLM extractor callable backed by Gemini.

    Args:
        model_name: The Gemini model to use (default: gemini-2.5-flash).

    Returns:
        A callable suitable for passing as ``llm_extractor`` to
        ``compose_retrieval_spec()``.

    Raises:
        ImportError: If google-genai is not installed.
        ValueError: If GEMINI_API_KEY is not set.
    """
    try:
        from google import genai
    except ImportError:
        raise ImportError(
            "google-genai package is required. Install with: pip install google-genai"
        )

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY environment variable is not set. "
            "Create a .env file with GEMINI_API_KEY=your_key"
        )

    client = genai.Client(api_key=api_key)

    def extract(ancillary_data: str, title: str) -> dict[str, object]:
        """Extract RetrievalSpec fields using Gemini."""
        prompt = _EXTRACTION_PROMPT.format(
            title=title,
            ancillary_data=ancillary_data,
        )

        logger.info("Calling Gemini (%s) for extraction...", model_name)

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config={
                "temperature": 0.0,
                "max_output_tokens": 2048,
            },
        )

        raw_text = response.text.strip()

        # Strip markdown code fences if present
        raw_text = _strip_markdown_fences(raw_text)

        logger.debug("Gemini raw response (%d chars): %s", len(raw_text), raw_text[:300])

        try:
            llm_result = json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error("Gemini returned invalid JSON: %s", e)
            logger.debug("Raw: %s", raw_text[:500])
            raise ValueError(f"Gemini returned invalid JSON: {e}") from e

        return _normalize_result(llm_result)

    return extract


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _normalize_result(raw: dict[str, object]) -> dict[str, object]:
    """Normalize Gemini's flat JSON into the format compose_retrieval_spec expects."""
    from src.retrieval.models import TargetWindow

    result: dict[str, object] = {}

    # String fields
    for field in (
        "source_type", "station_url", "station_code",
        "measurement", "aggregation", "unit",
    ):
        val = raw.get(field)
        result[field] = str(val) if val else ""

    # Precision — Gemini sometimes returns 0 for "whole degrees"; floor at 1
    precision = raw.get("precision")
    if isinstance(precision, (int, float)):
        result["precision"] = max(1, int(precision))
    else:
        result["precision"] = 1

    # Timezone
    tz = raw.get("timezone")
    result["timezone"] = str(tz) if tz else "UTC"

    # Target window
    start_str = str(raw.get("target_window_start", ""))
    end_str = str(raw.get("target_window_end", ""))

    if start_str and end_str:
        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d")
            end_dt = datetime.strptime(end_str, "%Y-%m-%d")
            result["target_window"] = TargetWindow(
                start=start_dt,
                end=end_dt.replace(hour=23, minute=59, second=59),
            )
        except (ValueError, TypeError):
            logger.warning("Could not parse LLM dates: %s / %s", start_str, end_str)

    # Finality after
    finality_str = str(raw.get("finality_after", ""))
    if finality_str:
        try:
            result["finality_after"] = datetime.strptime(finality_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            logger.warning("Could not parse LLM finality_after: %s", finality_str)

    return result
