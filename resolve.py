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
import json
import logging
import sys

from dotenv import load_dotenv

# Load .env before any other imports that read env vars
load_dotenv()

from src.observability.logging import configure_logging
from src.observability.tracing import flush as flush_langfuse
from src.orchestration.runner import PipelineRunner

logger = logging.getLogger("otb-resolver")


def _print_results_table(run, gold_path: str | None = None) -> None:
    """Print a table of results: case_id, expected, recommendation, confidence."""
    # Load gold answers if provided
    gold_by_id: dict[str, str] = {}
    if gold_path:
        try:
            with open(gold_path, encoding="utf-8") as f:
                gold = json.load(f)
            for g in gold.get("results", gold):
                gold_by_id[g["case_id"]] = g.get("recommendation", "?")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logging.getLogger("otb-resolver").warning(
                "Could not load gold answers from %s: %s", gold_path, e
            )

    has_gold = bool(gold_by_id)

    # Build header
    header_parts = ["Case ID", "Rec", "Conf"]
    if has_gold:
        header_parts.insert(2, "Expected")

    # Determine column widths
    case_id_width = max(
        max((len(ctx.case.case_id) for ctx in run.results), default=0),
        len("Case ID"),
    )

    # Build and print table
    sep = "-" * (case_id_width + 24 + (12 if has_gold else 0))
    print(f"\n{'=' * (case_id_width + 24 + (12 if has_gold else 0))}")
    if has_gold:
        print(f"{'Case ID':<{case_id_width}}  Expected  Rec     Conf")
    else:
        print(f"{'Case ID':<{case_id_width}}  Rec     Conf")
    print(sep)

    for ctx in run.results:
        case_id = ctx.case.case_id
        rec = ctx.resolution.recommendation if ctx.resolution else "unclear"
        conf = ctx.resolution.confidence if ctx.resolution else 0.0

        # Colorize match/mismatch if gold available
        if has_gold:
            expected = gold_by_id.get(case_id, "?")
            match_indicator = "✓" if rec == expected else "✗"
            print(
                f"{case_id:<{case_id_width}}  "
                f"{expected:<8}  {rec:<6} {conf:.2f}  {match_indicator}"
            )
        else:
            print(
                f"{case_id:<{case_id_width}}  {rec:<6} {conf:.2f}"
            )

    print(sep)

    # Print match summary if gold available
    if has_gold:
        matched = sum(
            1 for ctx in run.results
            if ctx.resolution
            and gold_by_id.get(ctx.case.case_id) == ctx.resolution.recommendation
        )
        total = len(run.results)
        print(f"Match: {matched}/{total} ({matched/total*100:.0f}%)" if total else "No cases")


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
    parser.add_argument(
        "--gold", default=None,
        help="Path to gold answers JSON for expected-value comparison (optional).",
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

        # Print detailed results table
        _print_results_table(run, args.gold)

        # Print summary footer
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
