"""PipelineContext — immutable state carrier that flows through each pipeline stage."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from src.validation.models import MarketCase
from src.retrieval.spec import RetrievalSpec
from src.retrieval.dispatch import RawObservationBatch


@dataclass(frozen=True)
class PipelineContext:
    """Immutable context passed through every pipeline stage.

    Each stage reads its input slot(s), computes its output, and returns
    a new PipelineContext with the output slot filled. If a stage fails
    fatally, it sets terminal=True and the runner stops.

    Attributes:
        case: The validated MarketCase (always present after Stage 1).
        spec: RetrievalSpec from Stage 2 (compose_spec).
        raw_batch: RawObservationBatch from Stage 3 (retrieval).
        normalized: NormalizedObservation from Stage 4 (normalization).
        verdict: ReconciliationVerdict from Stage 5 (reconciliation).
        resolution: Resolution from Stage 6 (decision).
        terminal: If True, the pipeline has short-circuited.
        terminal_reason: Why the pipeline stopped (e.g., "p4_too_early", "unclear").
        terminal_error: The exception that caused termination, if any.
        stage: Name of the last completed stage (for debugging).
    """

    # ── Stage outputs (None until the stage runs) ──
    case: MarketCase
    spec: Optional[RetrievalSpec] = None
    raw_batch: Optional[RawObservationBatch] = None
    normalized: Optional["NormalizedObservation"] = None   # forward ref
    verdict: Optional["ReconciliationVerdict"] = None      # forward ref
    resolution: Optional["Resolution"] = None              # forward ref

    # ── Terminal state ──
    terminal: bool = False
    terminal_reason: str = ""
    terminal_error: Optional[Exception] = None
    stage: str = "validate"

    def replace(self, **kwargs) -> "PipelineContext":
        """Return a new PipelineContext with the given fields replaced."""
        return replace(self, **kwargs)


# Forward references for type hints — these are imported lazily to avoid circulars.
# The actual classes are defined in their respective stage modules.
class NormalizedObservation:
    pass

class ReconciliationVerdict:
    pass

class Resolution:
    pass
