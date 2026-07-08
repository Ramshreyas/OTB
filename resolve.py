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
import logging
import sys

from dotenv import load_dotenv

# Load .env before any other imports that read env vars
load_dotenv()

from src.observability.logging import configure_logging
from src.observability.tracing import flush as flush_langfuse
from src.orchestration.runner import PipelineRunner

logger = logging.getLogger("otb-resolver")


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

        # Print summary
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
