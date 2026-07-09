#!/usr/bin/env python3
"""Seed Langfuse Prompt Registry with the two prompts used by the resolver.

Usage:
    python scripts/seed_langfuse_prompts.py

This creates (or updates) prompts in the connected Langfuse instance:
    1. weather-spec-extraction  (chat, label=production)
    2. weather-reviewer         (chat, label=production)

Requires Langfuse env vars (LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST)
to be set — reads from .env automatically.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

from langfuse import Langfuse

# ── Prompt definitions ────────────────────────────────────────────────

SPEC_EXTRACTION_PROMPT = [
    {
        "role": "user",
        "content": (
            "Extract these fields from the weather market resolution instructions below "
            "as a single JSON object. Return ONLY valid JSON, no markdown fences, no extra text.\n"
            "\n"
            "Required fields:\n"
            "- source_type: \"wunderground_station\" if wunderground.com, \"noaa_monthly\" if weather.gov or noaa.gov\n"
            "- station_url: exact URL from the text\n"
            "- station_code: 4-letter ICAO code from the URL (e.g. RJTT, KBKF, RKSI)\n"
            "- target_window_start: date as YYYY-MM-DD\n"
            "- target_window_end: same as start for single-day markets, as YYYY-MM-DD\n"
            "- measurement: one of [temperature, precipitation, wind_speed, wind_gust, humidity, visibility, pressure, snow, uv_index, cloud_cover, dew_point]\n"
            "- aggregation: \"min\" for lowest/minimum, \"max\" for highest/maximum, \"sum\" for total/precipitation, \"point\" for specific timestamp\n"
            "- unit: \"C\" for Celsius, \"F\" for Fahrenheit, \"in\" for inches, \"mm\" for millimeters\n"
            "- precision: integer decimal places. \"whole degrees\" = 1. \"2 decimal places\" = 2. \"3 decimal places\" = 3.\n"
            "- timezone: IANA timezone like \"Asia/Tokyo\", \"America/Denver\", \"Asia/Seoul\", \"Pacific/Auckland\"\n"
            "- finality_after: YYYY-MM-DD, day after window_end (markets cannot resolve until next-day data published)\n"
            "\n"
            "Title: {{title}}\n"
            "\n"
            "Ancillary data:\n"
            "{{ancillary_data}}"
        ),
    },
]

REVIEWER_PROMPT = [
    {
        "role": "user",
        "content": (
            "Review this weather market resolution for errors.\n"
            "\n"
            "Market: {{title}}\n"
            "Station: {{station}} ({{station_code}})\n"
            "Measurement: {{measurement}} {{aggregation}} over {{window}}\n"
            "Normalized value: {{value}}{{unit}} (expected precision: {{precision}}, completeness: {{completeness}})\n"
            "Quality flags: {{quality_flags}}\n"
            "Deterministic recommendation: {{recommendation}} (confidence: {{confidence}})\n"
            "Reasoning: {{reasoning}}\n"
            "\n"
            "Do you see any errors in the evidence chain, logical flaw in the comparison, "
            "or reason this market should be unclear instead?\n"
            "\n"
            "Answer ONLY with a JSON object: "
            '{"agree": true, "reasoning": "..."} or '
            '{"agree": false, "reasoning": "specific error found"}'
        ),
    },
]


def main():
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    host = os.getenv("LANGFUSE_HOST", "")

    if not public_key or not secret_key or not host:
        print("Error: Langfuse env vars not set.", file=sys.stderr)
        print("  LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST", file=sys.stderr)
        sys.exit(1)

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=host,
    )

    prompts = [
        ("weather-spec-extraction", SPEC_EXTRACTION_PROMPT),
        ("weather-reviewer", REVIEWER_PROMPT),
    ]

    for name, content in prompts:
        try:
            client.create_prompt(
                name=name,
                prompt=content,
                labels=["production"],
                type="chat",
                commit_message="Initial seed via scripts/seed_langfuse_prompts.py",
            )
            print(f"✓ Created prompt: {name} (label: production)")
        except Exception as e:
            print(f"✗ Failed to create '{name}': {e}")

    print("\nDone. Verify at {}/project/.../prompts".format(host))


if __name__ == "__main__":
    main()
