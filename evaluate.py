#!/usr/bin/env python3
"""Evaluation script — compares resolver output against gold answers.

Evaluates correctness, conservatism, evidence quality, source selection,
and decision path. Produces a per-case table and summary statistics.

Usage:
    python evaluate.py --predictions output/results.json --gold gold_visible/answers.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ── Column widths ──────────────────────────────────────────────────────

CASE_ID_W = 44
REC_W = 6
CONF_W = 5
COMPL_W = 6
PATH_W = 14
SOURCE_W = 9
FLAGS_W = 22

TABLE_SEP = "-" * (CASE_ID_W + REC_W + REC_W + REC_W + CONF_W + COMPL_W + PATH_W + SOURCE_W + FLAGS_W + 12)


def main():
    parser = argparse.ArgumentParser(description="Evaluate resolver output against gold answers.")
    parser.add_argument("--predictions", required=True, help="Path to resolver output JSON.")
    parser.add_argument("--gold", required=True, help="Path to gold answers JSON.")
    args = parser.parse_args()

    with open(args.predictions, encoding="utf-8") as f:
        preds = json.load(f)

    with open(args.gold, encoding="utf-8") as f:
        gold = json.load(f)

    gold_by_id = {g["case_id"]: g for g in gold.get("results", gold)}
    results = preds.get("results", [])

    # ── Per-case evaluation ──
    rows: list[dict] = []
    for pred in results:
        case_id = pred["case_id"]
        gold_case = gold_by_id.get(case_id)
        if gold_case is None:
            continue

        pred_rec = pred.get("recommendation", "unclear")
        gold_rec = gold_case.get("recommendation", "")
        confidence = pred.get("confidence", 0.0)
        decision_path = pred.get("decision_path", "?")
        review_reason = pred.get("review_reason")

        evidence = pred.get("evidence", {})
        obs_count = evidence.get("observation_count", 0)
        completeness = evidence.get("completeness", 0.0)
        station_code = evidence.get("station_code", "?")

        source_trace = pred.get("source_trace", [])
        source_paths = _source_paths(source_trace)
        source_errors = _source_errors(source_trace)

        # ── Flags ──
        flags: list[str] = []

        # Reviewer invoked?
        if review_reason or decision_path == "llm_reviewed":
            flags.append("reviewer")

        # Low completeness?
        if 0 < completeness < 0.5:
            flags.append(f"partial({completeness:.0%})")

        # Source errors?
        if source_errors:
            flags.append("src-err")

        # Confidence below reviewer threshold?
        if 0 < confidence < 0.85 and decision_path == "deterministic":
            flags.append("low-conf")

        # Wrong station? (heuristic: compare to expected from gold reasoning)
        gold_reasoning = gold_case.get("reasoning", "")
        if station_code != "?" and gold_reasoning and station_code not in gold_reasoning:
            flags.append("station?")

        rows.append({
            "case_id": case_id,
            "gold_rec": gold_rec,
            "pred_rec": pred_rec,
            "match": pred_rec == gold_rec,
            "confidence": confidence,
            "decision_path": decision_path,
            "obs_count": obs_count,
            "completeness": completeness,
            "station_code": station_code,
            "source_paths": source_paths,
            "source_errors": source_errors,
            "flags": flags,
            "review_reason": review_reason,
        })

    # ── Print per-case table ──
    print(f"\n{'='*len(TABLE_SEP)}")
    header = (
        f"{'Case ID':<{CASE_ID_W}}  "
        f"{'Exp':<{REC_W}}  {'Rec':<{REC_W}}  "
        f"{'Conf':>{CONF_W}}  {'Compl':>{COMPL_W}}  "
        f"{'Path':<{PATH_W}}  {'Source':<{SOURCE_W}}  "
        f"{'Flags':<{FLAGS_W}}"
    )
    print(header)
    print(TABLE_SEP)

    for row in rows:
        case_id = row["case_id"][:CASE_ID_W]
        match_mark = "✓" if row["match"] else "✗"
        compl_str = f"{row['completeness']:.0%}" if row["completeness"] > 0 else "—"
        path_short = _path_short(row["decision_path"])
        source_short = "/".join(row["source_paths"]) if row["source_paths"] else "?"
        flags_str = ", ".join(row["flags"]) if row["flags"] else "—"

        print(
            f"{case_id:<{CASE_ID_W}}  "
            f"{row['gold_rec']:<{REC_W}}  "
            f"{row['pred_rec']:<{REC_W}} {match_mark} "
            f"{row['confidence']:>{CONF_W}.2f}  "
            f"{compl_str:>{COMPL_W}}  "
            f"{path_short:<{PATH_W}}  "
            f"{source_short:<{SOURCE_W}}  "
            f"{flags_str:<{FLAGS_W}}"
        )

    print(TABLE_SEP)

    # ── Summary statistics ──
    total = len(rows)
    correct = sum(1 for r in rows if r["match"])
    false_confident = sum(1 for r in rows if not r["match"] and r["confidence"] >= 0.7)
    false_low_conf = sum(1 for r in rows if not r["match"] and r["confidence"] < 0.7)
    unclear_count = sum(1 for r in rows if r["pred_rec"] in ("unclear", "p3"))
    reviewer_count = sum(1 for r in rows if "reviewer" in r["flags"])
    partial_count = sum(1 for r in rows if any("partial" in f for f in r["flags"]))
    src_err_count = sum(1 for r in rows if r["source_errors"])

    print(f"\nAccuracy")
    print(f"  Correct:              {correct}/{total} ({_pct(correct, total)})")
    print(f"  False confident (BAD): {false_confident}/{total}")
    print(f"  False low confidence:  {false_low_conf}/{total}")
    print(f"  Conservative (unclear): {unclear_count}/{total}")
    print()
    print(f"Evidence Quality")
    print(f"  Avg completeness:      {_avg_completeness(rows):.0%}")
    print(f"  Low completeness (<50%): {partial_count}/{total}")
    print(f"  Reviewer invoked:       {reviewer_count}/{total}")
    print(f"  Source errors:          {src_err_count}/{total}")
    print()
    print(f"Decision Paths")
    for path in sorted(set(r["decision_path"] for r in rows)):
        count = sum(1 for r in rows if r["decision_path"] == path)
        avg_conf = _avg([r["confidence"] for r in rows if r["decision_path"] == path])
        print(f"  {_path_short(path):<{PATH_W}}  {count}/{total}  avg conf: {avg_conf:.2f}")
    print()
    print(f"Source Paths")
    all_sources: dict[str, int] = {}
    for row in rows:
        for sp in row["source_paths"]:
            all_sources[sp] = all_sources.get(sp, 0) + 1
    for sp, count in sorted(all_sources.items()):
        print(f"  {sp:<{SOURCE_W}}  {count}")

    # ── Warnings ──
    warnings: list[str] = []
    for row in rows:
        # Low completeness + high confidence = potential overconfidence
        if row["completeness"] < 0.5 and row["confidence"] > 0.7:
            warnings.append(
                f"  {row['case_id']}: completeness={row['completeness']:.0%} "
                f"but confidence={row['confidence']:.2f} — overconfident?"
            )
        # Reviewer disagreed or was invoked
        if row["review_reason"]:
            warnings.append(
                f"  {row['case_id']}: review_reason='{row['review_reason'][:80]}'"
            )
        # Source errors present
        if row["source_errors"]:
            warnings.append(
                f"  {row['case_id']}: source errors — {', '.join(row['source_errors'][:2])}"
            )
        # Wrong station heuristic
        if any("station?" in f for f in row["flags"]):
            warnings.append(
                f"  {row['case_id']}: station '{row['station_code']}' not found in gold reasoning"
            )

    if warnings:
        print(f"\n⚠ Warnings ({len(warnings)})")
        for w in warnings:
            print(w)

    print(f"\n{'='*len(TABLE_SEP)}")


# ── Helpers ────────────────────────────────────────────────────────────


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0.0%"
    return f"{n / total * 100:.1f}%"


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _avg_completeness(rows: list[dict]) -> float:
    vals = [r["completeness"] for r in rows if r["completeness"] > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _path_short(path: str) -> str:
    """Shorten decision path for display."""
    mapping = {
        "deterministic": "deterministic",
        "llm_reviewed": "llm-reviewed",
        "error": "error",
    }
    return mapping.get(path, path[:PATH_W])


def _source_paths(source_trace: list[dict]) -> list[str]:
    """Extract unique source paths from source trace entries."""
    seen = set()
    paths = []
    for entry in source_trace:
        p = entry.get("path", "?")
        if p not in seen:
            seen.add(p)
            paths.append(p)
        # Also capture guardrail flags
        for gf in entry.get("guardrail_flags", []):
            if gf not in seen:
                seen.add(gf)
                paths.append(gf)
    return paths


def _source_errors(source_trace: list[dict]) -> list[str]:
    """Extract errors from source trace entries."""
    errors = []
    for entry in source_trace:
        err = entry.get("error")
        if err:
            errors.append(err[:80])
    return errors


if __name__ == "__main__":
    main()
