"""Retrieval layer — spec composition, station registry, guardrails, fallbacks."""

from src.retrieval.models import (
    TargetWindow,
    CrossValidationResult,
    ExtractionMethod,
    SOURCE_TYPES,
    MEASUREMENT_TYPES,
    AGGREGATION_TYPES,
    VALID_UNITS,
)
from src.retrieval.spec import (
    RetrievalSpec,
    SpecGatingError,
    compose_retrieval_spec,
)
from src.retrieval.station_registry import (
    StationInfo,
    get_station_info,
    lookup_by_icao_code,
    lookup_by_url,
    STATION_REGISTRY,
)
from src.retrieval.guardrails import (
    get_guardrails,
)
from src.retrieval.regex_fallback import (
    extract_with_regex,
    RegexExtractionResult,
)

__all__ = [
    # Models
    "TargetWindow",
    "CrossValidationResult",
    "ExtractionMethod",
    "SOURCE_TYPES",
    "MEASUREMENT_TYPES",
    "AGGREGATION_TYPES",
    "VALID_UNITS",
    # Spec
    "RetrievalSpec",
    "SpecGatingError",
    "compose_retrieval_spec",
    # Station registry
    "StationInfo",
    "get_station_info",
    "lookup_by_icao_code",
    "lookup_by_url",
    "STATION_REGISTRY",
    # Guardrails
    "get_guardrails",
    # Regex fallback
    "extract_with_regex",
    "RegexExtractionResult",
]
