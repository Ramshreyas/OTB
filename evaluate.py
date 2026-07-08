#!/usr/bin/env python3
"""Evaluation script — compares resolver output against gold answers.

Usage:
    python evaluate.py --predictions output/results.json --gold gold_visible/answers.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Evaluate resolver output against gold answers.")
    parser.add_argument("--predictions", required=True, help="Path to resolver output JSON.")
    parser.add_argument("--gold", required=True, help="Path to gold answers JSON.")
    args = parser.parse_args()

    with open(args.predictions, encoding="utf-8") as f:
        preds = json.load(f)

    with open(args.gold, encoding="utf-8") as f:
        gold = json.load(f)

    # Index gold by case_id
    gold_by_id = {g["case_id"]: g for g in gold.get("results", gold)}

    total = 0
    correct = 0
    false_confident = 0
    false_unclear = 0
    conservative_saves = 0  # unclear that would have been wrong

    for pred in preds.get("results", []):
        case_id = pred["case_id"]
        gold_case = gold_by_id.get(case_id)
        if gold_case is None:
            continue

        total += 1
        pred_rec = pred.get("recommendation", "unclear")
        gold_rec = gold_case.get("recommendation", "")
        confidence = pred.get("confidence", 0.0)

        if pred_rec == gold_rec:
            correct += 1
        elif pred_rec in ("unclear", "p3"):
            # Conservative: returned unclear instead of guessing
            conservative_saves += 1
        elif confidence >= 0.7:
            # Wrong and confident — worst failure mode
            false_confident += 1
        else:
            # Wrong but low confidence — less bad
            false_unclear += 1

    print(f"\n{'='*60}")
    print("Evaluation Results")
    print(f"{'='*60}")
    print(f"Total cases:           {total}")
    print(f"Correct:               {correct} ({_pct(correct, total)})")
    print(f"Conservative (unclear): {conservative_saves} ({_pct(conservative_saves, total)})")
    print(f"False confident (BAD):  {false_confident}")
    print(f"False unclear:          {false_unclear}")
    print(f"{'='*60}")


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0.0%"
    return f"{n / total * 100:.1f}%"


if __name__ == "__main__":
    main()
