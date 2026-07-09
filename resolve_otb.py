#!/usr/bin/env python3
"""OTB Weather Market Resolver — Live OTB Mode.

Fetches live Weather markets from the OTB Oracle API and runs the full
resolution pipeline against them. This is the bonus extension described
in the case study: treat OTB API rows as production cases, run the same
resolver pipeline, and paper-propose recommendations.

Usage:
    # Fetch up to 10 proposed markets and resolve them
    python resolve_otb.py --max-markets 10

    # Fetch up to 50 proposed markets, larger page size
    python resolve_otb.py --max-markets 50 --page-size 100

    # Fetch settled markets for backtesting
    python resolve_otb.py --status settled --max-markets 5

    # Poll continuously every 5 minutes (Ctrl-C to stop)
    python resolve_otb.py --poll --poll-interval 300 --max-markets 20

    # Single case by case_id
    python resolve_otb.py --case-id "highest-temperature-in-denver-on-july-8-2026_0x29aed4"
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load .env before any other imports that read env vars
load_dotenv()

from src.observability.logging import configure_logging
from src.observability.tracing import flush as flush_langfuse
from src.orchestration.runner import PipelineRunner
from src.otb.api import OTBClient, OTBAPIError, OTBMarketItem, OTBFetchResult
from src.otb.transform import otb_items_to_market_cases

logger = logging.getLogger("otb-resolver")


# ── Globals for graceful shutdown ──────────────────────────────────────

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    logger.info("Received signal %d — shutting down gracefully...", signum)
    _shutdown_requested = True


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


# ── Main functions ─────────────────────────────────────────────────────


def fetch_and_resolve(
    *,
    client: OTBClient,
    runner: PipelineRunner,
    status: str,
    page_size: int,
    max_items: int,
    output_dir: Path,
    fixtures_dir: str,
    case_id: str | None,
    mode: str = "live",
) -> dict:
    """Fetch markets from OTB API and run the resolution pipeline.

    All orchestration: fetch → transform → run pipeline → persist results.

    Args:
        client: Configured OTBClient.
        runner: Configured PipelineRunner.
        status: Market status filter.
        page_size: Items per API page.
        max_items: Max markets to fetch and resolve.
        output_dir: Directory for results and raw payloads.
        fixtures_dir: Directory for fixture files.
        case_id: Optional case_id to filter to a single market.
        mode: Pipeline mode ("live" or "replay").

    Returns:
        Dict with run summary and per-market results.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw_otb_payloads"
    cases_dir = output_dir / "otb_cases"

    # ── Step 1: Fetch from OTB API ──
    logger.info("Fetching OTB markets (status=%s, max=%d, page_size=%d)...",
                status, max_items, page_size)
    try:
        fetch_result: OTBFetchResult = client.fetch_weather_markets(
            status=status,
            page_size=page_size,
            max_items=max_items,
            persist_raw_dir=raw_dir,
        )
    except OTBAPIError as e:
        logger.error("OTB API fetch failed: %s", e)
        sys.exit(1)

    items = fetch_result.items
    logger.info("Fetched %d items from OTB API (total available: %d, %.0fms)",
                len(items), fetch_result.total, fetch_result.latency_ms)

    if not items:
        logger.warning("No markets returned from OTB API. Check status filter or API availability.")
        return {
            "run_id": "empty",
            "fetched_at": fetch_result.fetched_at,
            "total_fetched": 0,
            "total_resolved": 0,
            "summary": {},
            "results": [],
        }

    # ── Step 2: Transform to MarketCase ──
    cases = otb_items_to_market_cases(items, status_filter=status)

    if not cases:
        logger.warning("No market cases could be transformed from OTB items.")
        return {
            "run_id": "empty",
            "fetched_at": fetch_result.fetched_at,
            "total_fetched": len(items),
            "total_resolved": 0,
            "summary": {},
            "results": [],
        }

    if case_id:
        cases = tuple(c for c in cases if c.case_id == case_id)
        if not cases:
            logger.error("Case '%s' not found in fetched markets.", case_id)
            sys.exit(1)
        logger.info("Filtered to single case: %s", case_id)

    # ── Step 3: Persist transformed cases as a manifest ──
    manifest_path = cases_dir / f"otb_manifest_{_now_str()}.json"
    _persist_manifest(cases, items, fetch_result, manifest_path)

    # ── Step 4: Run the pipeline ──
    run = runner.run_cases(
        cases=cases,
        mode=mode,
        fixtures_dir=fixtures_dir,
        case_id=None,  # Already filtered above
    )

    # ── Step 5: Write results ──
    results_path = output_dir / f"results_{run.run_id}.json"
    runner.write_results(run, results_path)

    # ── Step 6: Return summary ──
    return {
        "run_id": run.run_id,
        "fetched_at": fetch_result.fetched_at,
        "total_fetched": len(items),
        "total_resolved": run.total_cases,
        "summary": run.summary,
        "results": [_ctx_to_summary(ctx) for ctx in run.results],
        "manifest_path": str(manifest_path),
        "results_path": str(results_path),
        "raw_dir": str(raw_dir),
    }


def poll_loop(
    *,
    client: OTBClient,
    runner: PipelineRunner,
    status: str,
    page_size: int,
    max_items: int,
    poll_interval: float,
    output_dir: Path,
    fixtures_dir: str,
    mode: str = "live",
) -> None:
    """Continuously poll the OTB API and resolve new markets.

    Runs until shutdown signal (SIGINT/SIGTERM) or KeyboardInterrupt.

    Args:
        client: Configured OTBClient.
        runner: Configured PipelineRunner.
        status: Market status filter.
        page_size: Items per API page.
        max_items: Max markets per poll cycle.
        poll_interval: Seconds between polls.
        output_dir: Directory for results.
        fixtures_dir: Directory for fixture files.
        mode: Pipeline mode.
    """
    logger.info("Starting OTB polling loop (interval=%ds, status=%s, max=%d)",
                poll_interval, status, max_items)
    cycle = 0

    while not _shutdown_requested:
        cycle += 1
        logger.info("=== Poll cycle %d ===", cycle)
        try:
            summary = fetch_and_resolve(
                client=client,
                runner=runner,
                status=status,
                page_size=page_size,
                max_items=max_items,
                output_dir=output_dir,
                fixtures_dir=fixtures_dir,
                case_id=None,
                mode=mode,
            )
            _print_poll_summary(cycle, summary)
        except Exception as e:
            logger.error("Poll cycle %d failed: %s", cycle, e, exc_info=True)

        if _shutdown_requested:
            break

        logger.info("Sleeping %ds until next poll...", poll_interval)
        # Sleep in small chunks to respond to signals promptly
        for _ in range(int(poll_interval)):
            if _shutdown_requested:
                break
            time.sleep(1)

    logger.info("Polling stopped after %d cycles.", cycle)


# ── Helpers ────────────────────────────────────────────────────────────


def _now_str() -> str:
    """Current UTC timestamp as a compact string."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _persist_manifest(
    cases: tuple,
    items: tuple[OTBMarketItem, ...],
    fetch_result: OTBFetchResult,
    path: Path,
) -> None:
    """Persist transformed cases as a markets.json-style manifest file.

    This preserves the fetched data for replay and debugging — operators
    can re-run resolve.py --input with this manifest for deterministic replay.

    Args:
        cases: Tuple of MarketCase objects.
        items: Original OTB API items.
        fetch_result: Fetch result metadata.
        path: Output file path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    markets = []
    for case in cases:
        qd = case.question_data

        # Build question_data dict, omitting None/null fields to satisfy schema
        qd_dict: dict = {
            "title": qd.title,
            "end_date_iso": qd.end_date_iso,
            "outcomes": {
                "p1": qd.outcomes.p1,
                "p2": qd.outcomes.p2,
                "p3": qd.outcomes.p3,
                "p4": qd.outcomes.p4,
            },
        }
        # Optional fields — only include if non-None and non-empty
        _set_if_truthy(qd_dict, "question_id", qd.question_id)
        _set_if_truthy(qd_dict, "market_id", qd.market_id)
        _set_if_truthy(qd_dict, "market_slug", qd.market_slug)
        _set_if_truthy(qd_dict, "gamma_slug", qd.gamma_slug)
        _set_if_truthy(qd_dict, "proposal_time", qd.proposal_time)
        _set_if_truthy(qd_dict, "processed_time", qd.processed_time)
        _set_if_truthy(qd_dict, "resolution_conditions", qd.resolution_conditions)
        _set_if_truthy(qd_dict, "proposed_outcome", qd.proposed_outcome)

        case_dict: dict = {
            "case_id": case.case_id,
            "polymarket_url": case.polymarket_url,
            "proposal_tx_hash": case.proposal_tx_hash,
            "question_data": qd_dict,
            "ancillary_data": case.ancillary_data,
        }
        _set_if_truthy(case_dict, "fixture_path", case.fixture_path)
        markets.append(case_dict)

    manifest = {
        "schema_version": "otb-weather-case-v1",
        "generated_at": fetch_result.fetched_at,
        "description": (
            f"OTB live fetch — {len(cases)} markets from Oracle API "
            f"(fetched {fetch_result.fetched_at}, "
            f"total available: {fetch_result.total})"
        ),
        "market_selection": {
            "source": "otb-oracle-api",
            "status_filter": items[0].status if items else "unknown",
            "fetch_url": "https://oracle.api.otb.uma.xyz/requests",
        },
        "markets": markets,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, default=str)

    logger.info("Persisted manifest with %d cases to %s", len(cases), path)


def _set_if_truthy(d: dict, key: str, value) -> None:
    """Set a key in dict only if the value is truthy (non-None, non-empty)."""
    if value:
        d[key] = value


def _ctx_to_summary(ctx) -> dict:
    """Extract a compact summary from a PipelineContext for console output."""
    d = {
        "case_id": ctx.case.case_id,
        "title": ctx.case.question_data.title,
        "polymarket_url": ctx.case.polymarket_url,
    }
    if ctx.resolution:
        d.update({
            "recommendation": ctx.resolution.recommendation,
            "confidence": ctx.resolution.confidence,
            "path": ctx.resolution.path,
        })
    else:
        d.update({
            "recommendation": "unclear",
            "confidence": 0.0,
            "path": "error",
            "reason": ctx.terminal_reason,
        })
    if ctx.raw_batch:
        d["finality"] = ctx.raw_batch.finality.status
    return d


def _print_poll_summary(cycle: int, summary: dict) -> None:
    """Print a compact poll-cycle summary to console."""
    print(f"\n{'='*60}")
    print(f"OTB Poll Cycle {cycle} — {summary['run_id']}")
    print(f"Fetched: {summary['total_fetched']}  Resolved: {summary['total_resolved']}")
    if summary.get("summary"):
        s = summary["summary"]
        print(f"  p1={s['p1']}  p2={s['p2']}  p3={s['p3']}  p4={s['p4']}  unclear={s['unclear']}")
    if summary.get("results_path"):
        print(f"Results: {summary['results_path']}")
    print(f"{'='*60}\n")


# ── CLI ────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="OTB Weather Market Resolver — Live OTB Mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python resolve_otb.py --max-markets 10
  python resolve_otb.py --max-markets 50 --page-size 100
  python resolve_otb.py --status settled --max-markets 5
  python resolve_otb.py --poll --poll-interval 300 --max-markets 20
  python resolve_otb.py --case-id "highest-temp-in-denver_0x29aed4"
        """,
    )
    parser.add_argument(
        "--max-markets", type=int, default=10,
        help="Maximum number of markets to fetch and resolve (default: 10).",
    )
    parser.add_argument(
        "--page-size", type=int, default=50,
        help="Number of items per OTB API page (1-100, default: 50).",
    )
    parser.add_argument(
        "--status", default="proposed",
        help="Market status filter: proposed, settled, or empty for all (default: proposed).",
    )
    parser.add_argument(
        "--poll", action="store_true",
        help="Run in continuous polling mode.",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=300,
        help="Seconds between polls in polling mode (default: 300 = 5 minutes).",
    )
    parser.add_argument(
        "--output-dir", default="output/otb",
        help="Directory for results, raw payloads, and manifests (default: output/otb).",
    )
    parser.add_argument(
        "--fixtures", default="data/fixtures",
        help="Directory for fixture files (default: data/fixtures).",
    )
    parser.add_argument(
        "--pipeline-config", default="config/pipeline.yaml",
        help="Path to pipeline YAML config (default: config/pipeline.yaml).",
    )
    parser.add_argument(
        "--case-id",
        help="Run only the specified case_id (for debugging).",
    )
    parser.add_argument(
        "--replay", action="store_true",
        help="Run pipeline in replay mode (uses fixtures instead of live retrieval).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO).",
    )
    parser.add_argument(
        "--api-base-url", default="https://oracle.api.otb.uma.xyz",
        help="OTB Oracle API base URL (default: https://oracle.api.otb.uma.xyz).",
    )

    args = parser.parse_args()

    # Configure structured logging
    configure_logging(level=args.log_level)

    logger.info("Starting OTB Live Resolver (status=%s, max=%d, page_size=%d)",
                args.status, args.max_markets, args.page_size)

    try:
        # Build pipeline runner (reuses existing config)
        runner = PipelineRunner.from_yaml(args.pipeline_config)

        # Build OTB API client
        client = OTBClient(base_url=args.api_base_url)

        output_dir = Path(args.output_dir)
        mode = "replay" if args.replay else "live"

        if args.poll:
            # ── Continuous polling mode ──
            poll_loop(
                client=client,
                runner=runner,
                status=args.status,
                page_size=min(args.page_size, 100),
                max_items=args.max_markets,
                poll_interval=args.poll_interval,
                output_dir=output_dir,
                fixtures_dir=args.fixtures,
                mode=mode,
            )
        else:
            # ── Single-shot mode ──
            summary = fetch_and_resolve(
                client=client,
                runner=runner,
                status=args.status,
                page_size=min(args.page_size, 100),
                max_items=args.max_markets,
                output_dir=output_dir,
                fixtures_dir=args.fixtures,
                case_id=args.case_id,
                mode=mode,
            )

            # Print summary
            print(f"\n{'='*60}")
            print(f"OTB Live Resolver — {summary['run_id']}")
            print(f"Fetched: {summary['total_fetched']}  Resolved: {summary['total_resolved']}")
            if summary.get("summary"):
                s = summary["summary"]
                print(f"  p1={s['p1']}  p2={s['p2']}  p3={s['p3']}  p4={s['p4']}  unclear={s['unclear']}")
            if summary.get("manifest_path"):
                print(f"Manifest: {summary['manifest_path']}")
            if summary.get("results_path"):
                print(f"Results: {summary['results_path']}")
            if summary.get("raw_dir"):
                print(f"Raw payloads: {summary['raw_dir']}")
            print(f"{'='*60}")

            # Print per-market results
            if summary.get("results"):
                _print_results_table(summary["results"])

    except Exception as e:
        logger.error("OTB live resolver failed: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        flush_langfuse()


def _print_results_table(results: list[dict]) -> None:
    """Print a compact results table with Polymarket URLs for easy comparison."""
    if not results:
        return

    title_width = min(max(len(r.get("title", "")) for r in results), 100)
    sep = "-" * (title_width + 24)
    print(f"\n{'Market':<{title_width}}  Rec     Conf   Finality")
    print(sep)
    for r in results:
        title = r.get("title", "?")[:title_width]
        rec = r.get("recommendation", "?")
        conf = r.get("confidence", 0.0)
        fin = r.get("finality", "?")
        url = r.get("polymarket_url", "")
        print(f"{title:<{title_width}}  {rec:<6} {conf:.2f}   {fin}")
        if url:
            print(f"  {url}")
        print("-" * (title_width + 24))


if __name__ == "__main__":
    main()
