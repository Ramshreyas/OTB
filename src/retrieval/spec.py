"""Compose Retrieval Spec — Stage 2 of the resolution pipeline.

Takes a validated MarketCase and produces a complete, structured RetrievalSpec
that tells the retrieval layer exactly what to fetch, where, and with what
guardrails.

Extraction strategy: LLM-first with deterministic regex fallback, enriched
by station registry, guarded with station-specific operational hints, and
cross-validated against question_data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.validation.models import MarketCase
from src.retrieval.models import (
    TargetWindow,
    CrossValidationResult,
    ExtractionMethod,
    SOURCE_TYPES,
    MEASUREMENT_TYPES,
    AGGREGATION_TYPES,
    VALID_UNITS,
)
from src.retrieval.station_registry import get_station_info, StationInfo
from src.retrieval.guardrails import get_guardrails
from src.retrieval.regex_fallback import extract_with_regex, RegexExtractionResult

logger = logging.getLogger(__name__)


# ── Data models ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrievalSpec:
    """Complete specification for the retrieval layer.

    Every field tells the retrieval layer exactly what to fetch and how
    to interpret the results. This is the output of Stage 2 and the input
    to Stage 3 (Retrieval).

    Attributes:
        source_type: The type of weather data source.
        station_url: Full URL to the station's data page/api.
        station_code: ICAO or internal station identifier (e.g., "RJTT").
        target_window: The time window for observations.
        measurement: The type of measurement (temperature, precipitation, etc.).
        aggregation: How to aggregate across the window (min, max, sum, point).
        unit: Expected unit of the observation (C, F, in, mm).
        precision: Number of decimal places for the final value.
        timezone: IANA timezone name for the station's local time.
        finality_after: Datetime after which the market can be resolved.
        guardrails: List of station-specific operational hints for the retrieval layer.
        extraction_method: How the fields were extracted (llm or regex).
        cross_validation: Result of cross-validation against question_data.
    """

    source_type: str
    station_url: str
    station_code: str
    target_window: TargetWindow
    measurement: str
    aggregation: str
    unit: str
    precision: int
    timezone: str
    finality_after: datetime
    guardrails: list[str] = field(default_factory=list)
    extraction_method: ExtractionMethod = ExtractionMethod.REGEX
    cross_validation: CrossValidationResult = field(default_factory=CrossValidationResult)

    def __post_init__(self):
        """Validate field invariants."""
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(
                f"source_type must be one of {sorted(SOURCE_TYPES)}, "
                f"got '{self.source_type}'"
            )
        if not self.station_url.strip():
            raise ValueError("station_url must not be empty")
        if not self.station_code.strip():
            raise ValueError("station_code must not be empty")
        if self.measurement not in MEASUREMENT_TYPES:
            raise ValueError(
                f"measurement must be one of {sorted(MEASUREMENT_TYPES)}, "
                f"got '{self.measurement}'"
            )
        if self.aggregation not in AGGREGATION_TYPES:
            raise ValueError(
                f"aggregation must be one of {sorted(AGGREGATION_TYPES)}, "
                f"got '{self.aggregation}'"
            )
        if self.unit not in VALID_UNITS:
            raise ValueError(
                f"unit must be one of {sorted(VALID_UNITS)}, "
                f"got '{self.unit}'"
            )
        if self.precision < 0:
            raise ValueError(f"precision must be >= 0, got {self.precision}")


# ── Exceptions ────────────────────────────────────────────────────────

class SpecGatingError(Exception):
    """Raised when the RetrievalSpec cannot be composed due to a hard gate failure.

    Attributes:
        case_id: The case_id that failed gating.
        reason: The specific gate that failed (station_url, target_window,
            measurement, source_type, unit).
        detail: Human-readable explanation.
    """

    def __init__(self, case_id: str, reason: str, detail: str):
        super().__init__(f"[{case_id}] Gating failed — {reason}: {detail}")
        self.case_id = case_id
        self.reason = reason
        self.detail = detail


# ── Main composition function ─────────────────────────────────────────

def compose_retrieval_spec(
    market_case: MarketCase,
    *,
    llm_extractor: Optional[callable] = None,
    use_litellm: bool = True,
) -> RetrievalSpec:
    """Compose a complete RetrievalSpec from a validated MarketCase.

    This is the entry point for Stage 2 of the pipeline. It:
    1. Attempts LLM extraction (if an extractor callable is provided)
    2. Falls back to regex extraction
    3. Enriches with station registry
    4. Attaches station guardrails
    5. Cross-validates against question_data
    6. Returns the spec or raises SpecGatingError on hard gate failures

    Args:
        market_case: A validated, immutable MarketCase from Stage 1.
        llm_extractor: Optional callable that takes (ancillary_data: str,
            title: str) and returns a dict of extracted fields. If None and
            use_litellm is True, creates a LiteLLM-based extractor automatically.
        use_litellm: If True (default) and llm_extractor is None, auto-create
            a LiteLLM-backed LLM extractor. Set to False to use regex only.

    Returns:
        A validated RetrievalSpec ready for the retrieval layer.

    Raises:
        SpecGatingError: If any hard gate fails (missing station URL,
            unparseable target window, unrecognized measurement or source type).
    """
    ancillary = market_case.ancillary_data
    title = market_case.question_data.title
    case_id = market_case.case_id
    end_date_iso = market_case.question_data.end_date_iso

    # ── Step 1-3: Extract fields (LLM or regex) ──
    extracted: dict[str, object] = {}
    extraction_method = ExtractionMethod.REGEX

    if llm_extractor is None and use_litellm:
        # Auto-create LiteLLM extractor if configured
        try:
            from src.retrieval.llm_extractor import create_litellm_extractor
            llm_extractor = create_litellm_extractor()
            logger.debug("[%s] Auto-created LiteLLM extractor.", case_id)
        except Exception as e:
            logger.warning(
                "[%s] Could not create LiteLLM extractor: %s. Falling back to regex only.",
                case_id, e,
            )

    if llm_extractor is not None:
        try:
            llm_result = llm_extractor(ancillary, title)
            if _validate_llm_result(llm_result):
                extracted = llm_result
                extraction_method = ExtractionMethod.LLM
                logger.info(
                    "[%s] LLM extraction succeeded for all fields.", case_id
                )
            else:
                logger.warning(
                    "[%s] LLM extraction produced non-conforming output; "
                    "falling back to regex.", case_id
                )
        except Exception as e:
            logger.warning(
                "[%s] LLM extraction failed: %s; falling back to regex.",
                case_id, e
            )

    # Fall back to regex if LLM didn't provide results
    if extraction_method == ExtractionMethod.REGEX or not extracted:
        regex_result = extract_with_regex(ancillary, title, end_date_iso)
        extracted = _regex_result_to_dict(regex_result)
        extraction_method = ExtractionMethod.REGEX

    # ── Step 4: Enrich with station registry ──
    station_info = _enrich_from_registry(extracted, case_id)
    if station_info:
        extracted["station_code"] = station_info.icao_code
        extracted["station_url"] = station_info.url or extracted.get("station_url", "")
        extracted["timezone"] = station_info.timezone or extracted.get("timezone", "UTC")

    # ── Step 5: Gate checks ──
    _apply_hard_gates(extracted, case_id)

    # ── Step 6: Attach guardrails ──
    guardrails = []
    if station_info:
        guardrails = get_guardrails(station_info.icao_code)

    # ── Step 7: Cross-validate against question_data ──
    cross_validation = _cross_validate(extracted, market_case, station_info)

    # If cross-validation finds a fundamental conflict, gate to unclear
    if not cross_validation.all_checks_pass:
        failures = []
        if not cross_validation.date_agreement:
            failures.append(f"date: {cross_validation.date_agreement_detail}")
        if not cross_validation.measurement_consistency:
            failures.append(f"measurement: {cross_validation.measurement_consistency_detail}")
        if failures:
            raise SpecGatingError(
                case_id,
                "cross_validation",
                "; ".join(failures),
            )

    # ── Build and return the spec ──
    spec = RetrievalSpec(
        source_type=str(extracted.get("source_type", "wunderground_station")),
        station_url=str(extracted.get("station_url", "")),
        station_code=str(extracted.get("station_code", "")),
        target_window=extracted["target_window"],  # type: ignore[index]
        measurement=str(extracted.get("measurement", "temperature")),
        aggregation=str(extracted.get("aggregation", "max")),
        unit=str(extracted.get("unit", "C")),
        precision=int(extracted.get("precision", 1)),
        timezone=str(extracted.get("timezone", "UTC")),
        finality_after=extracted["finality_after"],  # type: ignore[index]
        guardrails=guardrails,
        extraction_method=extraction_method,
        cross_validation=cross_validation,
    )

    logger.info(
        "[%s] RetrievalSpec composed: source=%s station=%s measurement=%s "
        "aggregation=%s unit=%s precision=%d tz=%s method=%s",
        case_id, spec.source_type, spec.station_code, spec.measurement,
        spec.aggregation, spec.unit, spec.precision, spec.timezone,
        spec.extraction_method.value,
    )

    return spec


# ── Internal helpers ──────────────────────────────────────────────────

def _validate_llm_result(result: dict[str, object]) -> bool:
    """Check that an LLM extraction result has all required fields with valid types.

    Args:
        result: Dict from the LLM extractor.

    Returns:
        True if the result is valid and can be used directly.
    """
    required_str_fields = [
        "source_type", "station_url", "measurement", "aggregation", "unit"
    ]
    for field in required_str_fields:
        val = result.get(field)
        if not isinstance(val, str) or not val.strip():
            logger.debug("LLM result missing or invalid field: %s", field)
            return False

    # Validate source_type
    if result["source_type"] not in SOURCE_TYPES:
        logger.debug("LLM result has invalid source_type: %s", result["source_type"])
        return False

    # Validate measurement
    if result.get("measurement") not in MEASUREMENT_TYPES:
        logger.debug("LLM result has invalid measurement: %s", result.get("measurement"))
        return False

    # Validate precision
    precision = result.get("precision")
    if not isinstance(precision, (int, float)) or precision < 0:
        logger.debug("LLM result has invalid precision: %s", precision)
        return False

    return True


def _regex_result_to_dict(regex_result: RegexExtractionResult) -> dict[str, object]:
    """Convert a RegexExtractionResult to a flat dict for spec construction.

    Args:
        regex_result: The result from regex extraction.

    Returns:
        A dict with all RetrievalSpec fields.
    """
    return {
        "source_type": regex_result.source_type,
        "station_url": regex_result.station_url,
        "station_code": regex_result.station_code,
        "target_window": regex_result.target_window,
        "measurement": regex_result.measurement,
        "aggregation": regex_result.aggregation,
        "unit": regex_result.unit,
        "precision": regex_result.precision,
        "timezone": regex_result.timezone,
        "finality_after": regex_result.finality_after,
    }


def _enrich_from_registry(
    extracted: dict[str, object],
    case_id: str,
) -> Optional[StationInfo]:
    """Look up station info from the registry and merge into extracted fields.

    Args:
        extracted: Current extracted field dict (mutated in place).
        case_id: For logging.

    Returns:
        StationInfo if found, None otherwise.
    """
    station_code = str(extracted.get("station_code", ""))
    station_url = str(extracted.get("station_url", ""))

    station_info = get_station_info(station_code=station_code, url=station_url)

    if station_info:
        logger.debug(
            "[%s] Station registry hit: %s → %s",
            case_id, station_info.icao_code, station_info.name,
        )
    else:
        logger.warning(
            "[%s] Station not found in registry: code='%s', url='%s'",
            case_id, station_code, station_url,
        )

    return station_info


def _apply_hard_gates(extracted: dict[str, object], case_id: str) -> None:
    """Apply hard gates — raise SpecGatingError if any fail.

    Args:
        extracted: Extracted field dict.
        case_id: For error reporting.

    Raises:
        SpecGatingError: If any hard gate fails.
    """
    # Gate: station URL
    station_url_raw = extracted.get("station_url")
    station_url = str(station_url_raw) if station_url_raw else ""
    if not station_url.strip():
        raise SpecGatingError(
            case_id, "station_url",
            "No station URL found in ancillary_data and not constructible from station code.",
        )

    # Gate: target window
    target_window = extracted.get("target_window")
    if target_window is None:
        raise SpecGatingError(
            case_id, "target_window",
            "Could not parse target date/window from ancillary_data or end_date_iso.",
        )

    # Gate: source type
    source_type = str(extracted.get("source_type", ""))
    if source_type not in SOURCE_TYPES:
        raise SpecGatingError(
            case_id, "source_type",
            f"Unrecognized source type '{source_type}'. "
            f"Expected one of {sorted(SOURCE_TYPES)}.",
        )

    # Gate: measurement
    measurement = str(extracted.get("measurement", ""))
    if measurement not in MEASUREMENT_TYPES:
        raise SpecGatingError(
            case_id, "measurement",
            f"Unrecognized measurement type '{measurement}'. "
            f"Expected one of {sorted(MEASUREMENT_TYPES)}.",
        )

    # Note: unit is NOT a hard gate — it's flagged and verified post-retrieval
    unit = str(extracted.get("unit", ""))
    if unit not in VALID_UNITS:
        logger.warning(
            "[%s] Unit '%s' not recognized; will verify post-retrieval.", case_id, unit
        )


def _cross_validate(
    extracted: dict[str, object],
    market_case: MarketCase,
    station_info: Optional[StationInfo],
) -> CrossValidationResult:
    """Cross-validate extracted fields against question_data.

    Args:
        extracted: Extracted field dict.
        market_case: The original MarketCase.
        station_info: Station info from registry (may be None).

    Returns:
        CrossValidationResult with details of each check.
    """
    result = CrossValidationResult()
    qd = market_case.question_data
    title = qd.title

    # 1. Date/window agreement
    target_window = extracted.get("target_window")
    end_date_iso = qd.end_date_iso
    if target_window is not None and end_date_iso:
        result = _check_date_agreement(result, target_window, end_date_iso)

    # 2. Unit consistency
    extracted_unit = str(extracted.get("unit", ""))
    result = _check_unit_consistency(result, extracted_unit, title)

    # 3. Measurement consistency
    extracted_measurement = str(extracted.get("measurement", ""))
    extracted_aggregation = str(extracted.get("aggregation", ""))
    result = _check_measurement_consistency(
        result, extracted_measurement, extracted_aggregation, title
    )

    # 4. Station ↔ city awareness
    if station_info and station_info.city_note:
        result.station_city_awareness = False
        result.station_city_awareness_detail = station_info.city_note
    else:
        result.station_city_awareness_detail = "Station matches title city or no note."

    # Aggregate: any fundamental disagreement gates to unclear
    if not result.date_agreement or not result.measurement_consistency:
        result.all_checks_pass = False

    return result


def _check_date_agreement(
    result: CrossValidationResult,
    target_window: object,
    end_date_iso: str,
) -> CrossValidationResult:
    """Check if the extracted target window agrees with end_date_iso."""
    try:
        # Parse end_date_iso to a date
        end_date_str = end_date_iso.replace("Z", "").split("T")[0]
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

        if isinstance(target_window, TargetWindow):
            tw = target_window
            if tw.start.date() <= end_date <= tw.end.date():
                result.date_agreement = True
                result.date_agreement_detail = (
                    f"end_date_iso={end_date} falls within window "
                    f"[{tw.start.date()}, {tw.end.date()}]"
                )
            else:
                result.date_agreement = False
                result.date_agreement_detail = (
                    f"end_date_iso={end_date} does NOT fall within window "
                    f"[{tw.start.date()}, {tw.end.date()}] — "
                    f"market metadata contradicts resolution instructions"
                )
        else:
            result.date_agreement_detail = "target_window is not a TargetWindow instance"
    except Exception as e:
        result.date_agreement = False
        result.date_agreement_detail = f"Could not parse end_date_iso: {e}"

    return result


def _check_unit_consistency(
    result: CrossValidationResult,
    extracted_unit: str,
    title: str,
) -> CrossValidationResult:
    """Check if the extracted unit matches what the title implies."""
    title_has_celsius = "°C" in title or "Celsius" in title
    title_has_fahrenheit = "°F" in title or "Fahrenheit" in title

    if extracted_unit == "C" and title_has_fahrenheit:
        result.unit_consistency = False
        result.unit_consistency_detail = (
            "Extracted unit is Celsius but title references Fahrenheit"
        )
    elif extracted_unit == "F" and title_has_celsius:
        result.unit_consistency = False
        result.unit_consistency_detail = (
            "Extracted unit is Fahrenheit but title references Celsius"
        )
    else:
        result.unit_consistency_detail = (
            f"Unit '{extracted_unit}' is consistent with title"
        )

    return result


def _check_measurement_consistency(
    result: CrossValidationResult,
    measurement: str,
    aggregation: str,
    title: str,
) -> CrossValidationResult:
    """Check if extracted measurement+aggregation matches title keywords."""
    title_lower = title.lower()

    # Map title keywords to expected (measurement, aggregation)
    if "lowest" in title_lower or "minimum" in title_lower:
        expected_agg = "min"
    elif "highest" in title_lower or "maximum" in title_lower:
        expected_agg = "max"
    elif "precipitation" in title_lower or "rain" in title_lower:
        expected_agg = "sum"
    else:
        # Can't determine from title alone — skip check
        result.measurement_consistency_detail = (
            f"No clear aggregation keyword in title; extracted={aggregation}"
        )
        return result

    if aggregation != expected_agg:
        result.measurement_consistency = False
        result.measurement_consistency_detail = (
            f"Title implies aggregation='{expected_agg}' but extracted "
            f"aggregation='{aggregation}'"
        )
    else:
        result.measurement_consistency_detail = (
            f"Extracted aggregation='{aggregation}' matches title keywords"
        )

    # Also check measurement type
    if "temperature" in title_lower or "°C" in title_lower or "°F" in title_lower:
        if measurement != "temperature":
            result.measurement_consistency = False
            result.measurement_consistency_detail += (
                f"; title implies temperature but extracted='{measurement}'"
            )

    return result
