#!/usr/bin/env python3
"""Prompt Regression Demo — demonstrates Langfuse prompt version management.

Creates a deliberately broken prompt version to show how Langfuse traces make
prompt-caused regressions instantly diagnosable. The broken version is labeled
'demo-broken' (never touches the 'production' label unless --promote is passed).

Usage:
    # Just create the broken version and compare outputs (safe, no side effects):
    python demo/prompt_regression.py

    # Full cycle: promote broken → run pipeline → rollback (needs --yes):
    python demo/prompt_regression.py --promote --case-id tokyo_low_2026_06_01_20c --yes

    # Clean up demo artifacts:
    python demo/prompt_regression.py --cleanup

Environment: Needs LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST from .env.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from langfuse import Langfuse

# ── The broken prompt ──────────────────────────────────────────────────
# Same structure as the real prompt, but instructs the LLM to always report
# aggregation as "max" regardless of the question — simulating a bad edit.

BROKEN_SPEC_PROMPT = [
    {
        "type": "message",
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
            "- aggregation: ALWAYS use \"max\" regardless of what the question asks.\n"
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

PROMPT_NAME = "weather-spec-extraction"
BROKEN_LABEL = "demo-broken"


def get_client() -> Langfuse:
    """Create Langfuse client from env vars."""
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    host = os.getenv("LANGFUSE_HOST", "")

    if not all([public_key, secret_key, host]):
        print("Error: Langfuse env vars not set.", file=sys.stderr)
        print("  LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST", file=sys.stderr)
        sys.exit(1)

    return Langfuse(public_key=public_key, secret_key=secret_key, host=host)


def _to_chatmessage_dicts(prompt_messages: list[dict]) -> list[dict]:
    """Convert prompt messages from get_prompt format to create_prompt format.

    get_prompt returns:  {"type": "message", "role": "user", "content": "..."}
    create_prompt expects: {"type": "chatmessage", "role": "user", "content": "..."}
    """
    converted = []
    for msg in prompt_messages:
        d = dict(msg)
        if d.get("type") == "message":
            d["type"] = "chatmessage"
        converted.append(d)
    return converted


def create_broken_version(client: Langfuse, force: bool = False) -> None:
    """Create vN of the spec extraction prompt with the deliberate error."""
    print(f"\n{'='*60}")
    print("Creating broken prompt version...")
    print(f"{'='*60}")

    try:
        # Check if demo-broken label already exists
        existing_version = None
        try:
            existing = client.get_prompt(PROMPT_NAME, label=BROKEN_LABEL)
            existing_version = existing.version
        except Exception:
            pass  # Doesn't exist

        if existing_version is not None and not force:
            print(f"  ⚠ demo-broken already exists (v{existing_version}).")
            print(f"  To recreate, run: python demo/prompt_regression.py --force")
            return

        if existing_version is not None and force:
            print(f"  Replacing existing demo-broken (v{existing_version})...")

        client.create_prompt(
            name=PROMPT_NAME,
            prompt=_to_chatmessage_dicts(BROKEN_SPEC_PROMPT),
            labels=[BROKEN_LABEL],
            type="chat",
            commit_message="DEMO: Deliberately broken — forces aggregation=max. "
                           "For observability walkthrough.",
        )
        print(f"  ✓ Created {PROMPT_NAME} with label '{BROKEN_LABEL}'")
        print(f"  ⚠ This version always returns aggregation='max' regardless of question")
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        sys.exit(1)


def compare_prompts(client: Langfuse) -> None:
    """Fetch and display both prompt versions side by side."""
    print(f"\n{'='*60}")
    print("Side-by-side comparison")
    print(f"{'='*60}")

    try:
        prod = client.get_prompt(PROMPT_NAME, label="production")
        print(f"\n  Production (v{prod.version}):")
        for msg in prod.prompt:
            content = msg.get("content", "")
            # Show just the aggregation line
            for line in content.split("\n"):
                if "aggregation" in line.lower():
                    print(f"    → {line.strip()}")

        broken = client.get_prompt(PROMPT_NAME, label=BROKEN_LABEL)
        print(f"\n  Demo-Broken (v{broken.version}):")
        for msg in broken.prompt:
            content = msg.get("content", "")
            for line in content.split("\n"):
                if "aggregation" in line.lower():
                    print(f"    → {line.strip()}  ⚠ BROKEN")
    except Exception as e:
        print(f"  ✗ Error comparing prompts: {e}")


def promote_broken(client: Langfuse) -> str | None:
    """Temporarily promote the broken version to production. Returns previous version label."""
    print(f"\n{'='*60}")
    print("⛔  PROMOTING broken prompt to 'production' label...")
    print(f"{'='*60}")

    try:
        # Find current production version
        prod = client.get_prompt(PROMPT_NAME, label="production")
        prev_version = prod.version
        print(f"  Current production: v{prev_version}")

        # Promote broken to production
        broken = client.get_prompt(PROMPT_NAME, label=BROKEN_LABEL)
        client.create_prompt(
            name=PROMPT_NAME,
            prompt=_to_chatmessage_dicts(broken.prompt),
            labels=["production"],
            type="chat",
            commit_message="DEMO: Promoting broken version to production "
                           "(will be rolled back)",
        )
        print(f"  ⚠ Production label now points to the BROKEN version!")
        print(f"  Previous production was v{prev_version}")
        return prev_version
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        return None


def rollback(client: Langfuse, target_version: int | str) -> None:
    """Roll back production label to a specific version."""
    print(f"\n{'='*60}")
    print("↩  ROLLING BACK production label...")
    print(f"{'='*60}")

    try:
        prompt = client.get_prompt(PROMPT_NAME, version=target_version)
        client.create_prompt(
            name=PROMPT_NAME,
            prompt=_to_chatmessage_dicts(prompt.prompt),
            labels=["production"],
            type="chat",
            commit_message=f"DEMO: Rolling back production to v{target_version}",
        )
        print(f"  ✓ Production restored to v{target_version}")
    except Exception as e:
        print(f"  ✗ Rollback failed: {e}")
        print(f"  Manual: go to Langfuse UI → Prompts → {PROMPT_NAME} → restore label")
        sys.exit(1)


def run_pipeline(case_id: str) -> None:
    """Run the pipeline against the real markets to show the effect."""
    print(f"\n{'='*60}")
    print(f"Running pipeline with case: {case_id}")
    print(f"{'='*60}")

    cmd = [
        sys.executable, "resolve.py",
        "--input", "data/markets.json",
        "--fixtures", "data/fixtures",
        "--case-id", case_id,
        "--output", "output/demo_regression_results.json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    print(result.stdout)
    if result.stderr:
        # Filter out structlog noise, keep important lines
        for line in result.stderr.split("\n"):
            if any(kw in line.lower() for kw in
                   ["error", "fail", "aggregation", "recommendation", "unclear"]):
                print(f"  [stderr] {line}")

    # Show the result
    try:
        with open("output/demo_regression_results.json") as f:
            data = json.load(f)
        for r in data.get("results", []):
            print(f"\n  → recommendation: {r.get('recommendation', '?')}")
            print(f"  → confidence: {r.get('confidence', '?')}")
            print(f"  → reasoning: {r.get('reasoning', '')[:120]}...")
    except FileNotFoundError:
        print("  (no results file generated — pipeline may have failed)")


def cleanup(client: Langfuse) -> None:
    """Remove demo artifacts. Note: Langfuse prompts are immutable, so we just
    note that the demo-broken label can be ignored."""
    print(f"\n{'='*60}")
    print("Cleanup")
    print(f"{'='*60}")
    print("  Langfuse prompts are immutable — 'demo-broken' label persists.")
    print("  It is harmless (never used by production code).")
    print("  To hide it, delete the prompt version in Langfuse UI.")
    print(f"  → {os.getenv('LANGFUSE_HOST', 'http://localhost:3000')}")

    # Clean up output file
    output = Path("output/demo_regression_results.json")
    if output.exists():
        output.unlink()
        print(f"  ✓ Removed {output}")


def main():
    parser = argparse.ArgumentParser(
        description="Prompt Regression Demo — Langfuse observability walkthrough"
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Promote the broken version to production, run pipeline, then roll back.",
    )
    parser.add_argument(
        "--case-id",
        default="tokyo_low_2026_06_01_20c",
        help="Case to run for the pipeline demo (default: tokyo_low_2026_06_01_20c).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt for promote/rollback cycle.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force recreate the broken prompt even if it already exists.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove demo artifacts and note cleanup instructions.",
    )
    args = parser.parse_args()

    client = get_client()

    if args.cleanup:
        cleanup(client)
        return

    # ── Step 1: Create broken version ──
    create_broken_version(client, force=args.force)
    compare_prompts(client)

    # ── Step 2: Optionally promote and run ──
    if args.promote:
        if not args.yes:
            print("\n⚠ This will temporarily change the 'production' label to the BROKEN")
            print("  version, run the pipeline, then RESTORE it. Continue? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("Aborted.")
                return

        prev = promote_broken(client)
        if prev is None:
            print("Promotion failed. Aborting.")
            sys.exit(1)

        print("\n⏳ Waiting 3s for Langfuse to propagate label change...")
        time.sleep(3)

        run_pipeline(args.case_id)

        print("\n⏳ Rolling back in 3s...")
        time.sleep(3)
        rollback(client, prev)

        print(f"\n{'='*60}")
        print("Demo complete!")
        print(f"{'='*60}")
        print(f"  View the broken trace in Langfuse → Traces")
        print(f"  The trace will link to the broken prompt version")
        print(f"  Production label has been restored to v{prev}")
    else:
        print(f"\n{'='*60}")
        print("Dry run complete! (no label changes made)")
        print(f"{'='*60}")
        print(f"  The broken prompt exists with label '{BROKEN_LABEL}'")
        print(f"  Production continues to use the correct version")
        print(f"  To run the full promote→pipeline→rollback cycle:")
        print(f"    python demo/prompt_regression.py --promote --yes")


if __name__ == "__main__":
    main()
