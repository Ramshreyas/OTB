"""Gate 1: Finality check."""

from __future__ import annotations

from src.retrieval.dispatch import RawObservationBatch
from src.reconciliation.models import ReconciliationVerdict


def check_finality(batch: RawObservationBatch) -> ReconciliationVerdict | None:
    """Check if the market's finality condition is met.

    If finality is not confirmed, returns a verdict of p4_too_early.
    Returns None if the gate is passed (finality confirmed).

    Args:
        batch: RawObservationBatch with finality field.

    Returns:
        ReconciliationVerdict if gate fails, None if passed.
    """
    status = batch.finality.status

    if status == "confirmed":
        return None  # Pass — proceed to gate 2

    if status == "not_yet":
        return ReconciliationVerdict(
            verdict="p4_too_early",
            gate="finality",
            reasoning="Finality gate: next-day datapoint not yet published on the resolution source.",
        )

    # status == "unknown" → conservative default: treat as p4
    return ReconciliationVerdict(
        verdict="p4_too_early",
        gate="finality",
        reasoning="Finality gate: could not verify next-day datapoint (fetch failed). "
                  "Conservative default: assume not final.",
    )
