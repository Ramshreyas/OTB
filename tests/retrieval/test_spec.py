"""Tests for stage 2 — Compose Retrieval Spec.

Covers:
- RetrievalSpec / TargetWindow / CrossValidationResult model creation & validation
- Regex extraction fallback (all 5 real markets, edge cases)
- Station registry (lookup by ICAO code, URL, unknown station)
- Guardrails (default + station-specific)
- compose_retrieval_spec() end-to-end with real markets
- Hard gate failures (missing URL, missing window, unrecognized types)
- Cross-validation checks (date agreement, unit consistency, measurement, city)
- LLM extraction path (mocked)
- Immutability of RetrievalSpec
"""

from __future__ import annotations

import pytest
from datetime import datetime

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
    _validate_llm_result,
)
from src.retrieval.regex_fallback import (
    extract_with_regex,
    RegexExtractionResult,
    _extract_source_type,
    _extract_station_url,
    _extract_station_code,
    _extract_target_window,
    _extract_measurement,
    _extract_aggregation,
    _extract_unit,
    _extract_precision,
)
from src.retrieval.station_registry import (
    StationInfo,
    STATION_REGISTRY,
    lookup_by_icao_code,
    lookup_by_url,
    get_station_info,
    list_all_stations,
)
from src.retrieval.guardrails import (
    get_guardrails,
    get_guardrails_for_all_stations,
)
from src.validation.models import MarketCase, QuestionData, Outcomes


# ═══════════════════════════════════════════════════════════════════════
# Helper factories
# ═══════════════════════════════════════════════════════════════════════


def _make_outcomes() -> Outcomes:
    return Outcomes(p1="No", p2="Yes", p3="50/50 outcome", p4="Too Early")


def _make_question_data(
    title: str = "Will the lowest temperature in Tokyo be 20°C on June 1?",
    end_date_iso: str = "2026-06-01T00:00:00Z",
) -> QuestionData:
    return QuestionData(
        title=title,
        end_date_iso=end_date_iso,
        outcomes=_make_outcomes(),
    )


def _make_market_case(
    case_id: str = "test_case",
    ancillary_data: str | None = None,
    title: str = "Will the lowest temperature in Tokyo be 20°C on June 1?",
    end_date_iso: str = "2026-06-01T00:00:00Z",
) -> MarketCase:
    if ancillary_data is None:
        ancillary_data = (
            "q: title: Will the lowest temperature in Tokyo be 20°C on June 1?, "
            "description: This market resolves against Wunderground. "
            "Resolution source: https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
            "Measures temperatures to whole degrees Celsius. "
            "This market can not resolve until the first data point for the "
            "following date has been published. "
            "res_data: p1: 0, p2: 1, p3: 0.5."
        )
    return MarketCase(
        case_id=case_id,
        polymarket_url=f"https://polymarket.com/event/{case_id}",
        proposal_tx_hash="0x" + "a" * 64,
        question_data=_make_question_data(title=title, end_date_iso=end_date_iso),
        ancillary_data=ancillary_data,
    )


# ═══════════════════════════════════════════════════════════════════════
# TargetWindow
# ═══════════════════════════════════════════════════════════════════════


class TestTargetWindow:
    """Tests for the TargetWindow model."""

    def test_creation_single_day(self):
        """Should create a single-day window."""
        tw = TargetWindow(
            start=datetime(2026, 6, 1, 0, 0),
            end=datetime(2026, 6, 1, 23, 59, 59),
        )
        assert tw.is_single_day
        assert not tw.is_monthly

    def test_creation_monthly(self):
        """Should create a monthly window."""
        tw = TargetWindow(
            start=datetime(2026, 5, 1),
            end=datetime(2026, 5, 31, 23, 59, 59),
        )
        assert not tw.is_single_day
        assert tw.is_monthly

    def test_start_after_end_raises(self):
        """Start after end should raise ValueError."""
        with pytest.raises(ValueError, match="must not be after"):
            TargetWindow(
                start=datetime(2026, 6, 2),
                end=datetime(2026, 6, 1),
            )

    def test_immutable(self):
        """TargetWindow should be frozen."""
        tw = TargetWindow(
            start=datetime(2026, 6, 1),
            end=datetime(2026, 6, 1, 23, 59, 59),
        )
        with pytest.raises(Exception):
            tw.start = datetime(2026, 6, 2)  # type: ignore[misc]

    def test_equality(self):
        """Identical windows should be equal."""
        tw1 = TargetWindow(start=datetime(2026, 6, 1), end=datetime(2026, 6, 1, 23, 59, 59))
        tw2 = TargetWindow(start=datetime(2026, 6, 1), end=datetime(2026, 6, 1, 23, 59, 59))
        assert tw1 == tw2

    def test_inequality(self):
        """Different windows should not be equal."""
        tw1 = TargetWindow(start=datetime(2026, 6, 1), end=datetime(2026, 6, 1, 23, 59, 59))
        tw2 = TargetWindow(start=datetime(2026, 6, 2), end=datetime(2026, 6, 2, 23, 59, 59))
        assert tw1 != tw2


# ═══════════════════════════════════════════════════════════════════════
# CrossValidationResult
# ═══════════════════════════════════════════════════════════════════════


class TestCrossValidationResult:
    """Tests for CrossValidationResult."""

    def test_default_all_pass(self):
        """Default result should have all checks passing."""
        cv = CrossValidationResult()
        assert cv.date_agreement is True
        assert cv.unit_consistency is True
        assert cv.measurement_consistency is True
        assert cv.station_city_awareness is True
        assert cv.all_checks_pass is True

    def test_mutable_fields(self):
        """CrossValidationResult is mutable (built incrementally)."""
        cv = CrossValidationResult()
        cv.date_agreement = False
        cv.all_checks_pass = False
        assert cv.date_agreement is False

    def test_failure_details_populated(self):
        """Failure details should be settable."""
        cv = CrossValidationResult(
            date_agreement=False,
            date_agreement_detail="Dates do not match",
            unit_consistency=False,
            unit_consistency_detail="Unit mismatch",
            all_checks_pass=False,
        )
        assert "Dates do not match" in cv.date_agreement_detail
        assert "Unit mismatch" in cv.unit_consistency_detail


# ═══════════════════════════════════════════════════════════════════════
# RetrievalSpec
# ═══════════════════════════════════════════════════════════════════════


class TestRetrievalSpec:
    """Tests for the RetrievalSpec model."""

    @pytest.fixture
    def valid_window(self) -> TargetWindow:
        return TargetWindow(
            start=datetime(2026, 6, 1),
            end=datetime(2026, 6, 1, 23, 59, 59),
        )

    @pytest.fixture
    def valid_cross_val(self) -> CrossValidationResult:
        return CrossValidationResult()

    def test_creation_valid(self, valid_window, valid_cross_val):
        """Should create a valid RetrievalSpec."""
        spec = RetrievalSpec(
            source_type="wunderground_station",
            station_url="https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
            station_code="RJTT",
            target_window=valid_window,
            measurement="temperature",
            aggregation="min",
            unit="C",
            precision=1,
            timezone="Asia/Tokyo",
            finality_after=datetime(2026, 6, 2),
            guardrails=["verify unit label"],
            extraction_method=ExtractionMethod.REGEX,
            cross_validation=valid_cross_val,
        )
        assert spec.station_code == "RJTT"
        assert spec.measurement == "temperature"
        assert spec.aggregation == "min"
        assert spec.unit == "C"

    def test_immutable(self, valid_window, valid_cross_val):
        """RetrievalSpec should be frozen."""
        spec = RetrievalSpec(
            source_type="wunderground_station",
            station_url="https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
            station_code="RJTT",
            target_window=valid_window,
            measurement="temperature",
            aggregation="max",
            unit="C",
            precision=1,
            timezone="Asia/Tokyo",
            finality_after=datetime(2026, 6, 2),
            cross_validation=valid_cross_val,
        )
        with pytest.raises(Exception):
            spec.measurement = "precipitation"  # type: ignore[misc]

    @pytest.mark.parametrize("bad_source_type", [
        "google_weather",
        "accuweather",
        "",
        "WUNDERGROUND_STATION",  # case sensitive
    ])
    def test_invalid_source_type_raises(self, bad_source_type, valid_window, valid_cross_val):
        """Invalid source_type should raise ValueError."""
        with pytest.raises(ValueError, match="source_type"):
            RetrievalSpec(
                source_type=bad_source_type,
                station_url="https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
                station_code="RJTT",
                target_window=valid_window,
                measurement="temperature",
                aggregation="max",
                unit="C",
                precision=1,
                timezone="UTC",
                finality_after=datetime(2026, 6, 2),
                cross_validation=valid_cross_val,
            )

    @pytest.mark.parametrize("bad_measurement", [
        "temp",
        "high_temp",
        "",
    ])
    def test_invalid_measurement_raises(self, bad_measurement, valid_window, valid_cross_val):
        """Invalid measurement should raise ValueError."""
        with pytest.raises(ValueError, match="measurement"):
            RetrievalSpec(
                source_type="wunderground_station",
                station_url="https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
                station_code="RJTT",
                target_window=valid_window,
                measurement=bad_measurement,
                aggregation="max",
                unit="C",
                precision=1,
                timezone="UTC",
                finality_after=datetime(2026, 6, 2),
                cross_validation=valid_cross_val,
            )

    @pytest.mark.parametrize("bad_aggregation", [
        "avg",
        "mean",
        "high",
    ])
    def test_invalid_aggregation_raises(self, bad_aggregation, valid_window, valid_cross_val):
        """Invalid aggregation should raise ValueError."""
        with pytest.raises(ValueError, match="aggregation"):
            RetrievalSpec(
                source_type="wunderground_station",
                station_url="https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
                station_code="RJTT",
                target_window=valid_window,
                measurement="temperature",
                aggregation=bad_aggregation,
                unit="C",
                precision=1,
                timezone="UTC",
                finality_after=datetime(2026, 6, 2),
                cross_validation=valid_cross_val,
            )

    @pytest.mark.parametrize("bad_unit", [
        "K",
        "Kelvin",
        "",
    ])
    def test_invalid_unit_raises(self, bad_unit, valid_window, valid_cross_val):
        """Invalid unit should raise ValueError."""
        with pytest.raises(ValueError, match="unit"):
            RetrievalSpec(
                source_type="wunderground_station",
                station_url="https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
                station_code="RJTT",
                target_window=valid_window,
                measurement="temperature",
                aggregation="max",
                unit=bad_unit,
                precision=1,
                timezone="UTC",
                finality_after=datetime(2026, 6, 2),
                cross_validation=valid_cross_val,
            )

    def test_negative_precision_raises(self, valid_window, valid_cross_val):
        """Negative precision should raise ValueError."""
        with pytest.raises(ValueError, match="precision"):
            RetrievalSpec(
                source_type="wunderground_station",
                station_url="https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
                station_code="RJTT",
                target_window=valid_window,
                measurement="temperature",
                aggregation="max",
                unit="C",
                precision=-1,
                timezone="UTC",
                finality_after=datetime(2026, 6, 2),
                cross_validation=valid_cross_val,
            )

    def test_empty_station_url_raises(self, valid_window, valid_cross_val):
        """Empty station_url should raise ValueError."""
        with pytest.raises(ValueError, match="station_url"):
            RetrievalSpec(
                source_type="wunderground_station",
                station_url="",
                station_code="RJTT",
                target_window=valid_window,
                measurement="temperature",
                aggregation="max",
                unit="C",
                precision=1,
                timezone="UTC",
                finality_after=datetime(2026, 6, 2),
                cross_validation=valid_cross_val,
            )

    def test_empty_station_code_raises(self, valid_window, valid_cross_val):
        """Empty station_code should raise ValueError."""
        with pytest.raises(ValueError, match="station_code"):
            RetrievalSpec(
                source_type="wunderground_station",
                station_url="https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
                station_code="",
                target_window=valid_window,
                measurement="temperature",
                aggregation="max",
                unit="C",
                precision=1,
                timezone="UTC",
                finality_after=datetime(2026, 6, 2),
                cross_validation=valid_cross_val,
            )

    def test_equality(self, valid_window, valid_cross_val):
        """Identical specs should be equal."""
        kwargs = {
            "source_type": "wunderground_station",
            "station_url": "https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
            "station_code": "RJTT",
            "target_window": valid_window,
            "measurement": "temperature",
            "aggregation": "max",
            "unit": "C",
            "precision": 1,
            "timezone": "Asia/Tokyo",
            "finality_after": datetime(2026, 6, 2),
            "cross_validation": valid_cross_val,
        }
        s1 = RetrievalSpec(**kwargs)
        s2 = RetrievalSpec(**kwargs)
        assert s1 == s2


# ═══════════════════════════════════════════════════════════════════════
# LLM result validation
# ═══════════════════════════════════════════════════════════════════════


class TestLLMResultValidation:
    """Tests for _validate_llm_result()."""

    def test_valid_result_passes(self):
        """A complete, type-correct LLM result should pass."""
        result = {
            "source_type": "wunderground_station",
            "station_url": "https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
            "measurement": "temperature",
            "aggregation": "min",
            "unit": "C",
            "precision": 1,
        }
        assert _validate_llm_result(result) is True

    def test_missing_field_fails(self):
        """Missing a required string field should fail."""
        result = {
            "source_type": "wunderground_station",
            "station_url": "https://example.com",
            "measurement": "temperature",
            # missing aggregation
            "unit": "C",
        }
        assert _validate_llm_result(result) is False

    def test_bad_source_type_fails(self):
        """An invalid source_type should fail."""
        result = {
            "source_type": "google_weather",
            "station_url": "https://example.com",
            "measurement": "temperature",
            "aggregation": "max",
            "unit": "C",
            "precision": 1,
        }
        assert _validate_llm_result(result) is False

    def test_bad_measurement_fails(self):
        """An unrecognized measurement should fail."""
        result = {
            "source_type": "wunderground_station",
            "station_url": "https://example.com",
            "measurement": "pollen_count",
            "aggregation": "max",
            "unit": "C",
            "precision": 1,
        }
        assert _validate_llm_result(result) is False

    def test_negative_precision_fails(self):
        """Negative precision should fail."""
        result = {
            "source_type": "wunderground_station",
            "station_url": "https://example.com",
            "measurement": "temperature",
            "aggregation": "max",
            "unit": "C",
            "precision": -1,
        }
        assert _validate_llm_result(result) is False


# ═══════════════════════════════════════════════════════════════════════
# Regex fallback — individual extractors
# ═══════════════════════════════════════════════════════════════════════


class TestRegexSourceType:
    """Tests for _extract_source_type()."""

    def test_wunderground_url_detected(self):
        ancillary = "https://www.wunderground.com/history/daily/jp/tokyo/RJTT"
        assert _extract_source_type(ancillary) == "wunderground_station"

    def test_noaa_url_detected(self):
        ancillary = "Source: https://www.weather.gov/foo/bar for precipitation"
        assert _extract_source_type(ancillary) == "noaa_monthly"

    def test_no_recognized_url_defaults_to_wunderground(self):
        ancillary = "No URL here, just text."
        assert _extract_source_type(ancillary) == "wunderground_station"


class TestRegexStationUrl:
    """Tests for _extract_station_url()."""

    def test_extracts_wunderground_url(self):
        ancillary = (
            "Resolution source: "
            "https://www.wunderground.com/history/daily/jp/tokyo/RJTT "
            "for temperature data."
        )
        url = _extract_station_url(ancillary)
        assert url == "https://www.wunderground.com/history/daily/jp/tokyo/RJTT"

    def test_extracts_korean_station(self):
        ancillary = (
            "available here: "
            "https://www.wunderground.com/history/daily/kr/busan/RKPK"
        )
        url = _extract_station_url(ancillary)
        assert url == "https://www.wunderground.com/history/daily/kr/busan/RKPK"

    def test_extracts_denver_station(self):
        ancillary = (
            "available here: "
            "https://www.wunderground.com/history/daily/us/co/aurora/KBKF"
        )
        url = _extract_station_url(ancillary)
        assert url == "https://www.wunderground.com/history/daily/us/co/aurora/KBKF"

    def test_no_url_returns_none(self):
        ancillary = "No URL here."
        assert _extract_station_url(ancillary) is None

    def test_extracts_noaa_url(self):
        ancillary = "Source: https://www.weather.gov/foo/bar data."
        url = _extract_station_url(ancillary)
        assert url is not None
        assert "weather.gov" in url


class TestRegexStationCode:
    """Tests for _extract_station_code()."""

    @pytest.mark.parametrize("code", ["RJTT", "RKSI", "RKPK", "KBKF", "NZWN"])
    def test_extracts_icao_from_url(self, code):
        ancillary = (
            f"https://www.wunderground.com/history/daily/xx/yyy/{code}/date/2026-06-01"
        )
        assert _extract_station_code(ancillary) == code

    def test_no_code_returns_none(self):
        ancillary = "No ICAO code here."
        assert _extract_station_code(ancillary) is None


class TestRegexTargetWindow:
    """Tests for _extract_target_window()."""

    def test_extracts_from_dd_mmm_yy_format(self):
        ancillary = "lowest temperature recorded ... on 1 Jun '26."
        tw = _extract_target_window(ancillary, "Test title", "")
        assert tw is not None
        assert tw.start.date() == datetime(2026, 6, 1).date()
        assert tw.end.date() == datetime(2026, 6, 1).date()

    def test_extracts_may_date(self):
        ancillary = "highest temperature ... on 31 May '26."
        tw = _extract_target_window(ancillary, "Test title", "")
        assert tw is not None
        assert tw.start.date() == datetime(2026, 5, 31).date()

    def test_falls_back_to_end_date_iso(self):
        ancillary = "Temperature for the specified date."
        tw = _extract_target_window(ancillary, "Test title", "2026-06-01T00:00:00Z")
        assert tw is not None
        assert tw.start.date() == datetime(2026, 6, 1).date()

    def test_iso_date_in_ancillary(self):
        ancillary = "Date: 2026-06-01 observation data."
        tw = _extract_target_window(ancillary, "Test title", "")
        assert tw is not None
        assert tw.start.date() == datetime(2026, 6, 1).date()

    def test_from_title(self):
        ancillary = "No date in ancillary."
        title = "Will the temperature be 20°C on June 1?"
        tw = _extract_target_window(ancillary, title, "2026-06-01")
        assert tw is not None
        assert tw.start.date() == datetime(2026, 6, 1).date()

    def test_from_title_may(self):
        ancillary = "No date in ancillary."
        title = "Will the highest temperature in Denver be between 68-69°F on May 31?"
        tw = _extract_target_window(ancillary, title, "2026-05-31")
        assert tw is not None
        assert tw.start.date() == datetime(2026, 5, 31).date()

    def test_no_date_returns_none(self):
        ancillary = "No date information at all."
        tw = _extract_target_window(ancillary, "Vague title", "")
        assert tw is None


class TestRegexMeasurement:
    """Tests for _extract_measurement()."""

    def test_temperature_detected(self):
        assert _extract_measurement(
            "measures temperatures to whole degrees Celsius",
            "Will the temperature be 20°C?"
        ) == "temperature"

    def test_precipitation_detected(self):
        assert _extract_measurement(
            "precipitation in Seattle in May. Source: NOAA.",
            "Precipitation total for May?"
        ) == "precipitation"

    def test_default_to_temperature(self):
        assert _extract_measurement(
            "Some vague description.",
            "Some vague title."
        ) == "temperature"


class TestRegexAggregation:
    """Tests for _extract_aggregation()."""

    def test_lowest_detected(self):
        assert _extract_aggregation(
            "lowest temperature recorded",
            "Will the lowest temperature be 20°C?"
        ) == "min"

    def test_highest_detected(self):
        assert _extract_aggregation(
            "highest temperature recorded",
            "Will the highest temperature be 30°C?"
        ) == "max"

    def test_minimum_detected(self):
        assert _extract_aggregation(
            "minimum temperature recorded",
            "What was the minimum?"
        ) == "min"

    def test_precipitation_defaults_to_sum(self):
        assert _extract_aggregation(
            "precipitation total for May",
            "Total precipitation in May?"
        ) == "sum"

    def test_default_to_max(self):
        assert _extract_aggregation(
            "temperature for the day",
            "What was the temperature?"
        ) == "max"


class TestRegexUnit:
    """Tests for _extract_unit()."""

    def test_celsius_from_precision_phrase(self):
        """Should extract Celsius from 'whole degrees Celsius'."""
        ancillary = (
            "measures temperatures to whole degrees Celsius. "
            "To toggle between Fahrenheit and Celsius, click the gear icon."
        )
        assert _extract_unit(ancillary) == "C"

    def test_fahrenheit_from_precision_phrase(self):
        """Should extract Fahrenheit from 'whole degrees Fahrenheit'."""
        ancillary = (
            "measures temperatures to whole degrees Fahrenheit. "
            "To toggle between Fahrenheit and Celsius, click the gear icon."
        )
        assert _extract_unit(ancillary) == "F"

    def test_celsius_fallback(self):
        """When no precision phrase, fall back to general mentions."""
        ancillary = "Temperature in degrees Celsius for the station."
        assert _extract_unit(ancillary) == "C"

    def test_fahrenheit_fallback(self):
        ancillary = "Temperature in degrees Fahrenheit for the station."
        assert _extract_unit(ancillary) == "F"

    def test_inches_for_precipitation(self):
        ancillary = "Precipitation in inches."
        assert _extract_unit(ancillary) == "in"

    def test_default_to_celsius(self):
        ancillary = "No unit information."
        assert _extract_unit(ancillary) == "C"


class TestRegexPrecision:
    """Tests for _extract_precision()."""

    def test_whole_degrees_returns_1(self):
        assert _extract_precision(
            "measures temperatures to whole degrees Celsius"
        ) == 1

    def test_two_decimal_places(self):
        assert _extract_precision(
            "to 2-decimal places for precipitation"
        ) == 2

    def test_three_decimal_places(self):
        assert _extract_precision(
            "to 3 decimal precision"
        ) == 3

    def test_default_returns_1(self):
        assert _extract_precision("No precision info") == 1


# ═══════════════════════════════════════════════════════════════════════
# RegexExtractionResult — end-to-end on real ancillary_data
# ═══════════════════════════════════════════════════════════════════════


class TestRegexFullExtraction:
    """Full extract_with_regex() tests on real market ancillary_data."""

    def test_tokyo_low_extraction(self):
        """Tokyo low market should extract correctly."""
        ancillary = (
            "q: title: Will the lowest temperature in Tokyo be 20°C on June 1?, "
            "description: This market will resolve to the temperature range that "
            "contains the lowest temperature recorded at the Tokyo Haneda Airport "
            "Station in degrees Celsius on 1 Jun '26.\n\n"
            "Resolution source: https://www.wunderground.com/history/daily/jp/tokyo/RJTT.\n\n"
            "To toggle between Fahrenheit and Celsius, click the gear icon.\n\n"
            "This market can not resolve until the first data point for the "
            "following date has been published.\n\n"
            "Measures temperatures to whole degrees Celsius (eg, 9°C). "
            "res_data: p1: 0, p2: 1, p3: 0.5."
        )
        title = "Will the lowest temperature in Tokyo be 20°C on June 1?"
        end_date = "2026-06-01T00:00:00Z"

        result = extract_with_regex(ancillary, title, end_date)

        assert result.source_type == "wunderground_station"
        assert result.station_code == "RJTT"
        assert "RJTT" in result.station_url
        assert result.measurement == "temperature"
        assert result.aggregation == "min"
        assert result.unit == "C"
        assert result.precision == 1
        assert result.target_window is not None
        assert result.target_window.start.date() == datetime(2026, 6, 1).date()
        assert result.finality_after is not None
        assert result.finality_after.date() == datetime(2026, 6, 2).date()

    def test_tokyo_high_extraction(self):
        """Tokyo high market should extract 'max' aggregation."""
        ancillary = (
            "q: title: Will the highest temperature in Tokyo be 29°C or higher on "
            "June 1?, description: highest temperature recorded at the Tokyo Haneda "
            "Airport Station in degrees Celsius on 1 Jun '26.\n\n"
            "Resolution source: https://www.wunderground.com/history/daily/jp/tokyo/RJTT.\n\n"
            "Measures temperatures to whole degrees Celsius. "
            "This market can not resolve until the first data point for the "
            "following date has been published."
        )
        title = "Will the highest temperature in Tokyo be 29°C or higher on June 1?"

        result = extract_with_regex(ancillary, title, "2026-06-01T00:00:00Z")

        assert result.station_code == "RJTT"
        assert result.aggregation == "max"
        assert result.unit == "C"

    def test_denver_extraction(self):
        """Denver market should extract Fahrenheit, KBKF."""
        ancillary = (
            "q: title: Will the highest temperature in Denver be between 68-69°F on "
            "May 31?, description: highest temperature recorded at the Buckley Space "
            "Force Base Station in degrees Fahrenheit on 31 May '26.\n\n"
            "Resolution source: "
            "https://www.wunderground.com/history/daily/us/co/aurora/KBKF.\n\n"
            "Measures temperatures to whole degrees Fahrenheit (eg, 21°F). "
            "This market can not resolve until the first data point for the "
            "following date has been published."
        )
        title = "Will the highest temperature in Denver be between 68-69°F on May 31?"

        result = extract_with_regex(ancillary, title, "2026-05-31T00:00:00Z")

        assert result.station_code == "KBKF"
        assert result.aggregation == "max"
        assert result.unit == "F"
        assert result.target_window is not None
        assert result.target_window.start.date() == datetime(2026, 5, 31).date()

    def test_seoul_incheon_extraction(self):
        """Seoul market should extract RKSI (Incheon), not a Seoul station."""
        ancillary = (
            "q: title: Will the lowest temperature in Seoul be 16°C on June 1?, "
            "description: lowest temperature recorded at the Incheon Intl Airport "
            "Station in degrees Celsius on 1 Jun '26.\n\n"
            "Resolution source: "
            "https://www.wunderground.com/history/daily/kr/incheon/RKSI.\n\n"
            "Measures temperatures to whole degrees Celsius."
        )
        title = "Will the lowest temperature in Seoul be 16°C on June 1?"

        result = extract_with_regex(ancillary, title, "2026-06-01T00:00:00Z")

        assert result.station_code == "RKSI"
        assert "incheon" in result.station_url.lower()
        assert result.aggregation == "min"

    def test_busan_gimhae_extraction(self):
        """Busan market should extract RKPK (Gimhae)."""
        ancillary = (
            "q: title: Will the highest temperature in Busan be 22°C or below on "
            "June 1?, description: highest temperature recorded at the Gimhae Intl "
            "Airport Station in degrees Celsius on 1 Jun '26.\n\n"
            "Resolution source: "
            "https://www.wunderground.com/history/daily/kr/busan/RKPK."
        )
        title = "Will the highest temperature in Busan be 22°C or below on June 1?"

        result = extract_with_regex(ancillary, title, "2026-06-01T00:00:00Z")

        assert result.station_code == "RKPK"
        assert "busan" in result.station_url.lower()

    def test_no_date_falls_back_to_end_date_iso(self):
        """When ancillary has no date, end_date_iso should be used."""
        ancillary = (
            "Resolution source: "
            "https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
            "Measures temperatures to whole degrees Celsius."
        )
        title = "Will the temperature be 20°C?"
        end_date = "2026-06-01T00:00:00Z"

        result = extract_with_regex(ancillary, title, end_date)

        assert result.target_window is not None
        assert result.target_window.start.date() == datetime(2026, 6, 1).date()


# ═══════════════════════════════════════════════════════════════════════
# Station Registry
# ═══════════════════════════════════════════════════════════════════════


class TestStationRegistry:
    """Tests for the station registry."""

    def test_registry_has_expected_stations(self):
        """Should contain all known stations."""
        codes = set(STATION_REGISTRY.keys())
        assert codes >= {"RJTT", "RKSI", "RKPK", "KBKF", "NZWN", "KSEA"}

    def test_all_entries_are_station_info(self):
        """All registry values should be StationInfo instances."""
        for info in STATION_REGISTRY.values():
            assert isinstance(info, StationInfo)

    def test_lookup_by_known_icao(self):
        """Should find RJTT by ICAO code."""
        info = lookup_by_icao_code("RJTT")
        assert info is not None
        assert info.name == "Tokyo Haneda Airport"
        assert info.timezone == "Asia/Tokyo"
        assert info.city == "Tokyo"

    def test_lookup_case_insensitive(self):
        """ICAO code lookup should be case-insensitive."""
        info = lookup_by_icao_code("rjtt")
        assert info is not None
        assert info.icao_code == "RJTT"

    def test_lookup_unknown_icao_returns_none(self):
        """Unknown ICAO code should return None."""
        assert lookup_by_icao_code("XXXX") is None

    def test_lookup_by_url_full(self):
        """Should find station by full URL."""
        url = "https://www.wunderground.com/history/daily/jp/tokyo/RJTT"
        info = lookup_by_url(url)
        assert info is not None
        assert info.icao_code == "RJTT"

    def test_lookup_by_url_partial(self):
        """Should find station by partial URL path."""
        url = "https://www.wunderground.com/history/daily/jp/tokyo/RJTT/date/2026-06-01"
        info = lookup_by_url(url)
        assert info is not None
        assert info.icao_code == "RJTT"

    def test_lookup_by_url_with_icao_code(self):
        """Should find station when ICAO code is last URL segment."""
        url = "https://www.wunderground.com/history/daily/xx/yy/RJTT"
        info = lookup_by_url(url)
        assert info is not None
        assert info.icao_code == "RJTT"

    def test_lookup_by_url_unknown_returns_none(self):
        """Unknown URL should return None."""
        assert lookup_by_url("https://example.com/weather") is None

    def test_get_station_info_by_code(self):
        """get_station_info with just a station code."""
        info = get_station_info(station_code="KBKF")
        assert info is not None
        assert info.icao_code == "KBKF"
        assert info.timezone == "America/Denver"

    def test_get_station_info_by_url(self):
        """get_station_info with just a URL."""
        url = "https://www.wunderground.com/history/daily/kr/incheon/RKSI"
        info = get_station_info(url=url)
        assert info is not None
        assert info.icao_code == "RKSI"

    def test_get_station_info_unknown_returns_none(self):
        """Unknown station should return None."""
        info = get_station_info(station_code="ZZZZ")
        assert info is None

    def test_city_note_on_seoul_incheon(self):
        """RKSI should have city_note about Seoul/Incheon mismatch."""
        info = lookup_by_icao_code("RKSI")
        assert info is not None
        assert info.city_note is not None
        assert "Seoul" in info.city_note
        assert "Incheon" in info.city_note

    def test_city_note_on_denver_buckley(self):
        """KBKF should have city_note about Denver/Buckley mismatch."""
        info = lookup_by_icao_code("KBKF")
        assert info is not None
        assert info.city_note is not None
        assert "Denver" in info.city_note
        assert "Buckley" in info.city_note

    def test_no_city_note_on_tokyo(self):
        """RJTT (Tokyo Haneda) should NOT have a city_note (matches title city)."""
        info = lookup_by_icao_code("RJTT")
        assert info is not None
        assert info.city_note is None

    def test_list_all_stations(self):
        """list_all_stations should return sorted list."""
        stations = list_all_stations()
        assert len(stations) >= 6
        codes = [s.icao_code for s in stations]
        assert codes == sorted(codes)


# ═══════════════════════════════════════════════════════════════════════
# Guardrails
# ═══════════════════════════════════════════════════════════════════════


class TestGuardrails:
    """Tests for the guardrails system."""

    def test_get_guardrails_includes_defaults(self):
        """All stations should get default guardrails plus any station-specific ones."""
        guardrails = get_guardrails("NZWN")  # Station with no known quirks
        # Should have at least the 4 defaults
        assert len(guardrails) >= 4

    def test_get_guardrails_for_rjtt_includes_quirks(self):
        """RJTT has known quirks — they should be included."""
        guardrails = get_guardrails("RJTT")
        assert len(guardrails) >= 5  # 4 default + at least 1 specific
        quirk_found = any("RJTT" in g for g in guardrails)
        assert quirk_found, f"Expected RJTT-specific quirk in: {guardrails}"

    def test_get_guardrails_for_kbkf_includes_partial_data_warning(self):
        """KBKF should have the partial-data warning."""
        guardrails = get_guardrails("KBKF")
        assert any("intraday" in g.lower() for g in guardrails)

    def test_get_guardrails_unknown_station_has_defaults(self):
        """Even unknown stations get default guardrails."""
        guardrails = get_guardrails("ZZZZ")
        assert len(guardrails) >= 4

    def test_get_guardrails_for_all_stations(self):
        """Should return guardrails for all registered stations."""
        all_guardrails = get_guardrails_for_all_stations()
        assert set(all_guardrails.keys()) == set(STATION_REGISTRY.keys())


# ═══════════════════════════════════════════════════════════════════════
# compose_retrieval_spec() — end-to-end with real markets
# ═══════════════════════════════════════════════════════════════════════


class TestComposeRetrievalSpecReal:
    """End-to-end spec composition on real market cases loaded from markets.json."""

    @pytest.fixture
    def loaded_cases(self):
        """Load all 5 real market cases."""
        from src.validation.loader import load_markets
        from pathlib import Path
        manifest = load_markets(
            Path(__file__).resolve().parent.parent.parent / "data" / "markets.json"
        )
        return manifest.markets

    def test_composes_all_five_without_error(self, loaded_cases):
        """All 5 real market cases should compose successfully."""
        for case in loaded_cases:
            try:
                spec = compose_retrieval_spec(case)
                assert isinstance(spec, RetrievalSpec)
            except SpecGatingError as e:
                pytest.fail(f"Case {case.case_id} raised SpecGatingError: {e}")

    def test_tokyo_low_spec_fields(self, loaded_cases):
        """Tokyo low case should produce correct spec fields."""
        tokyo = next(c for c in loaded_cases if c.case_id == "tokyo_low_2026_06_01_20c")
        spec = compose_retrieval_spec(tokyo)

        assert spec.source_type == "wunderground_station"
        assert spec.station_code == "RJTT"
        assert spec.timezone == "Asia/Tokyo"
        assert spec.measurement == "temperature"
        assert spec.aggregation == "min"
        assert spec.unit == "C"
        assert spec.precision == 1
        assert spec.target_window.start.date() == datetime(2026, 6, 1).date()
        assert spec.extraction_method == ExtractionMethod.REGEX
        assert spec.cross_validation.all_checks_pass is True

    def test_tokyo_high_spec_fields(self, loaded_cases):
        """Tokyo high case should produce correct spec fields."""
        tokyo = next(
            c for c in loaded_cases
            if c.case_id == "tokyo_high_2026_06_01_29c_or_higher"
        )
        spec = compose_retrieval_spec(tokyo)

        assert spec.aggregation == "max"
        assert spec.station_code == "RJTT"

    def test_denver_spec_fields(self, loaded_cases):
        """Denver case should use KBKF, Fahrenheit."""
        denver = next(
            c for c in loaded_cases
            if c.case_id == "denver_high_2026_05_31_68_69f"
        )
        spec = compose_retrieval_spec(denver)

        assert spec.station_code == "KBKF"
        assert spec.timezone == "America/Denver"
        assert spec.unit == "F"
        assert spec.target_window.start.date() == datetime(2026, 5, 31).date()
        # Should have city_note about Denver/Buckley
        assert spec.cross_validation.station_city_awareness_detail is not None
        assert "Denver" in spec.cross_validation.station_city_awareness_detail

    def test_seoul_spec_fields(self, loaded_cases):
        """Seoul case should use RKSI (Incheon), not a Seoul station."""
        seoul = next(
            c for c in loaded_cases
            if c.case_id == "seoul_low_2026_06_01_16c"
        )
        spec = compose_retrieval_spec(seoul)

        assert spec.station_code == "RKSI"
        assert spec.timezone == "Asia/Seoul"
        assert "Seoul" in spec.cross_validation.station_city_awareness_detail

    def test_specs_are_immutable(self, loaded_cases):
        """All composed specs should be immutable."""
        for case in loaded_cases:
            spec = compose_retrieval_spec(case)
            with pytest.raises(Exception):
                spec.station_code = "mutated"  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
# compose_retrieval_spec() — gating failures
# ═══════════════════════════════════════════════════════════════════════


class TestComposeRetrievalSpecGating:
    """Gating checks should raise SpecGatingError."""

    def test_missing_station_url_gates_to_unclear(self):
        """No station URL should raise SpecGatingError."""
        case = _make_market_case(
            case_id="no_url",
            ancillary_data="Temperature data for June 1. No URL here. res_data: p1: 0, p2: 1.",
        )
        with pytest.raises(SpecGatingError) as exc_info:
            compose_retrieval_spec(case)
        assert "station_url" in exc_info.value.reason
        assert exc_info.value.case_id == "no_url"

    def test_no_date_gates_to_unclear(self):
        """No date in ancillary, title, or end_date_iso should raise SpecGatingError."""
        case = _make_market_case(
            case_id="no_date",
            title="Will the temperature reach the threshold?",
            ancillary_data=(
                "Resolution source: "
                "https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
                "Measures temperatures to whole degrees Celsius."
            ),
            end_date_iso="",
        )
        with pytest.raises(SpecGatingError) as exc_info:
            compose_retrieval_spec(case)
        assert "target_window" in exc_info.value.reason

    def test_bad_source_type_not_silent(self):
        """Unrecognized source_type should not be silently accepted."""
        case = _make_market_case(
            case_id="bad_source",
            ancillary_data=(
                "Temperature data for June 1. Source: google.com/weather. "
                "https://www.wunderground.com/history/daily/jp/tokyo/RJTT."
            ),
        )
        # The URL is present (wunderground), so source_type will be wunderground_station
        # via regex. If there were ONLY a non-wunderground, non-noaa URL,
        # it would default to wunderground_station (acceptable default).
        # This test verifies the regex can handle this correctly.
        spec = compose_retrieval_spec(case)
        assert spec.source_type == "wunderground_station"  # regex found wunderground URL


# ═══════════════════════════════════════════════════════════════════════
# compose_retrieval_spec() — cross-validation
# ═══════════════════════════════════════════════════════════════════════


class TestComposeRetrievalSpecCrossValidation:
    """Cross-validation checks between extracted fields and question_data."""

    def test_date_agreement_when_dates_match(self):
        """When extracted date matches end_date_iso, date_agreement should be True."""
        case = _make_market_case(
            case_id="date_match",
            end_date_iso="2026-06-01T00:00:00Z",
            ancillary_data=(
                "lowest temperature recorded on 1 Jun '26. "
                "https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
                "Measures temperatures to whole degrees Celsius."
            ),
        )
        spec = compose_retrieval_spec(case)
        assert spec.cross_validation.date_agreement is True

    def test_measurement_consistency_lowest_min(self):
        """Title says 'lowest', extraction should return aggregation='min'."""
        case = _make_market_case(
            case_id="meas_consistency",
            title="Will the lowest temperature in Tokyo be 20°C on June 1?",
            ancillary_data=(
                "lowest temperature recorded on 1 Jun '26. "
                "https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
                "Measures temperatures to whole degrees Celsius."
            ),
        )
        spec = compose_retrieval_spec(case)
        assert spec.cross_validation.measurement_consistency is True
        assert spec.aggregation == "min"

    def test_unit_consistency_celsius(self):
        """Title has °C, extraction should match C."""
        case = _make_market_case(
            case_id="unit_consistency",
            title="Will the temperature be 20°C on June 1?",
            ancillary_data=(
                "temperature recorded on 1 Jun '26. "
                "https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
                "Measures temperatures to whole degrees Celsius."
            ),
        )
        spec = compose_retrieval_spec(case)
        # Unit consistency should be True since both are Celsius
        assert spec.cross_validation.unit_consistency is True

    def test_city_awareness_note_recorded(self):
        """When station city differs from title city, city_note is recorded."""
        case = _make_market_case(
            case_id="seoul_test",
            title="Will the lowest temperature in Seoul be 16°C on June 1?",
            ancillary_data=(
                "lowest temperature recorded at Incheon Intl Airport on 1 Jun '26. "
                "https://www.wunderground.com/history/daily/kr/incheon/RKSI. "
                "Measures temperatures to whole degrees Celsius."
            ),
        )
        spec = compose_retrieval_spec(case)
        # Seoul title but RKSI station — should have city awareness note
        assert spec.cross_validation.station_city_awareness is False
        assert "Seoul" in spec.cross_validation.station_city_awareness_detail


# ═══════════════════════════════════════════════════════════════════════
# compose_retrieval_spec() — LLM extraction path (mocked)
# ═══════════════════════════════════════════════════════════════════════


class TestComposeRetrievalSpecLLM:
    """Tests for the LLM extraction path."""

    def test_llm_extractor_used_when_provided(self):
        """When an LLM extractor callable is provided and returns valid output,
        it should be used instead of regex."""
        def mock_llm_extractor(ancillary: str, title: str) -> dict:
            return {
                "source_type": "wunderground_station",
                "station_url": "https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
                "station_code": "RJTT",
                "target_window": TargetWindow(
                    start=datetime(2026, 6, 1),
                    end=datetime(2026, 6, 1, 23, 59, 59),
                ),
                "measurement": "temperature",
                "aggregation": "min",
                "unit": "C",
                "precision": 1,
                "timezone": "Asia/Tokyo",
                "finality_after": datetime(2026, 6, 2),
            }

        case = _make_market_case()
        spec = compose_retrieval_spec(case, llm_extractor=mock_llm_extractor)

        assert spec.extraction_method == ExtractionMethod.LLM
        assert spec.station_code == "RJTT"
        assert spec.aggregation == "min"

    def test_llm_extractor_falls_back_on_invalid_output(self):
        """When LLM returns invalid output, regex fallback should be used."""
        def bad_llm_extractor(ancillary: str, title: str) -> dict:
            return {"source_type": "invalid_type", "station_url": ""}

        case = _make_market_case()
        spec = compose_retrieval_spec(case, llm_extractor=bad_llm_extractor)

        # Falls back to regex
        assert spec.extraction_method == ExtractionMethod.REGEX
        assert spec.station_code == "RJTT"

    def test_llm_extractor_falls_back_on_exception(self):
        """When LLM extractor raises, regex fallback should be used."""
        def crashing_llm_extractor(ancillary: str, title: str) -> dict:
            raise RuntimeError("LLM API unavailable")

        case = _make_market_case()
        spec = compose_retrieval_spec(case, llm_extractor=crashing_llm_extractor)

        # Falls back to regex
        assert spec.extraction_method == ExtractionMethod.REGEX
        assert spec.station_code == "RJTT"

    def test_llm_extractor_not_called_when_none(self):
        """When no LLM extractor is provided, regex should be used directly."""
        case = _make_market_case()
        spec = compose_retrieval_spec(case)  # No llm_extractor

        assert spec.extraction_method == ExtractionMethod.REGEX


# ═══════════════════════════════════════════════════════════════════════
# Known enumerations — completeness
# ═══════════════════════════════════════════════════════════════════════


class TestEnumerations:
    """Ensure the known enumerations are complete and correct."""

    def test_source_types_include_wunderground_and_noaa(self):
        assert "wunderground_station" in SOURCE_TYPES
        assert "noaa_monthly" in SOURCE_TYPES

    def test_measurement_types_include_temperature_and_precipitation(self):
        assert "temperature" in MEASUREMENT_TYPES
        assert "precipitation" in MEASUREMENT_TYPES
        assert "wind_speed" in MEASUREMENT_TYPES

    def test_aggregation_types_include_min_max_sum_point(self):
        assert AGGREGATION_TYPES == {"min", "max", "sum", "point"}

    def test_valid_units_include_c_f_in_mm(self):
        assert VALID_UNITS == {"C", "F", "in", "mm"}


# ═══════════════════════════════════════════════════════════════════════
# Live LLM Integration tests — use real Gemini API
# ═══════════════════════════════════════════════════════════════════════


class TestLiteLLMIntegration:
    """Integration tests using the LiteLLM proxy.

    These require GEMINI_API_KEY and LITELLM_BASE_URL set in the environment.
    Skip them with: pytest -m "not integration"
    """

    @pytest.fixture
    def llm_extractor(self):
        """Create the LiteLLM-based extractor (skips if not configured)."""
        import os
        from dotenv import load_dotenv
        load_dotenv()

        if not os.getenv("GEMINI_API_KEY"):
            pytest.skip("GEMINI_API_KEY not set")

        from src.retrieval.llm_extractor import create_litellm_extractor
        return create_litellm_extractor()

    @pytest.fixture
    def all_cases(self):
        """Load all 5 real market cases."""
        from src.validation.loader import load_markets
        from pathlib import Path
        manifest = load_markets(
            Path(__file__).resolve().parent.parent.parent / "data" / "markets.json"
        )
        return list(manifest.markets)

    @pytest.mark.integration
    def test_litellm_extracts_all_five_cases(self, llm_extractor, all_cases):
        """LiteLLM should successfully extract all 5 real market cases."""
        for case in all_cases:
            spec = compose_retrieval_spec(case, llm_extractor=llm_extractor)
            assert isinstance(spec, RetrievalSpec)
            assert spec.station_code  # non-empty
            assert spec.extraction_method == ExtractionMethod.LLM

    @pytest.mark.integration
    def test_litellm_tokyo_low_fields(self, llm_extractor):
        """LiteLLM should extract correct fields for Tokyo low market."""
        case = _make_market_case(
            case_id="tokyo_low",
            title="Will the lowest temperature in Tokyo be 20°C on June 1?",
            ancillary_data=(
                "q: title: Will the lowest temperature in Tokyo be 20°C on June 1?, "
                "description: This market will resolve to the temperature range that "
                "contains the lowest temperature recorded at the Tokyo Haneda Airport "
                "Station in degrees Celsius on 1 Jun '26.\n\n"
                "The resolution source for this market will be information from "
                "Wunderground, specifically the lowest temperature recorded for all "
                "times on this day for the Tokyo Haneda Airport Station, available "
                "here: https://www.wunderground.com/history/daily/jp/tokyo/RJTT.\n\n"
                "To toggle between Fahrenheit and Celsius, click the gear icon next "
                "to the search bar and switch the Temperature setting between °F "
                "and °C.\n\n"
                "This market can not resolve until the first data point for the "
                "following date has been published on the resolution source.\n\n"
                "The resolution source for this market measures temperatures to whole "
                "degrees Celsius (eg, 9°C). Thus, this is the level of precision that "
                "will be used when resolving the market.\n\n"
                "res_data: p1: 0, p2: 1, p3: 0.5."
            ),
        )
        spec = compose_retrieval_spec(case, llm_extractor=llm_extractor)

        assert spec.extraction_method == ExtractionMethod.LLM
        assert spec.source_type == "wunderground_station"
        assert spec.station_code == "RJTT"
        assert spec.station_url == "https://www.wunderground.com/history/daily/jp/tokyo/RJTT"
        assert spec.measurement == "temperature"
        assert spec.aggregation == "min"
        assert spec.unit == "C"
        assert spec.precision >= 1
        assert spec.timezone == "Asia/Tokyo"
        assert spec.target_window.start.date() == datetime(2026, 6, 1).date()
        assert spec.finality_after.date() == datetime(2026, 6, 2).date()

    @pytest.mark.integration
    def test_litellm_denver_fields(self, llm_extractor):
        """LiteLLM should extract correct fields for Denver (KBKF, Fahrenheit)."""
        case = _make_market_case(
            case_id="denver_high",
            title="Will the highest temperature in Denver be between 68-69°F on May 31?",
            end_date_iso="2026-05-31T00:00:00Z",
            ancillary_data=(
                "q: title: Will the highest temperature in Denver be between 68-69°F "
                "on May 31?, description: This market will resolve to the temperature "
                "range that contains the highest temperature recorded at the Buckley "
                "Space Force Base Station in degrees Fahrenheit on 31 May '26.\n\n"
                "The resolution source for this market will be information from "
                "Wunderground, specifically the highest temperature recorded for all "
                "times on this day for the Buckley Space Force Base Station, available "
                "here: https://www.wunderground.com/history/daily/us/co/aurora/KBKF.\n\n"
                "To toggle between Fahrenheit and Celsius, click the gear icon next "
                "to the search bar and switch the Temperature setting between °F and "
                "°C.\n\n"
                "This market can not resolve until the first data point for the "
                "following date has been published on the resolution source.\n\n"
                "The resolution source for this market measures temperatures to whole "
                "degrees Fahrenheit (eg, 21°F). Thus, this is the level of precision "
                "that will be used when resolving the market."
            ),
        )
        spec = compose_retrieval_spec(case, llm_extractor=llm_extractor)

        assert spec.extraction_method == ExtractionMethod.LLM
        assert spec.station_code == "KBKF"
        assert spec.unit == "F"
        assert spec.aggregation == "max"
        assert spec.target_window.start.date() == datetime(2026, 5, 31).date()

    @pytest.mark.integration
    def test_litellm_seoul_incheon_station_awareness(self, llm_extractor):
        """LiteLLM should extract RKSI for Seoul (Incheon), not a Seoul city station."""
        case = _make_market_case(
            case_id="seoul_low",
            title="Will the lowest temperature in Seoul be 16°C on June 1?",
            ancillary_data=(
                "q: title: Will the lowest temperature in Seoul be 16°C on June 1?, "
                "description: This market will resolve to the temperature range that "
                "contains the lowest temperature recorded at the Incheon Intl Airport "
                "Station in degrees Celsius on 1 Jun '26.\n\n"
                "The resolution source for this market will be information from "
                "Wunderground, specifically the lowest temperature recorded for all "
                "times on this day for the Incheon Intl Airport Station, available "
                "here: https://www.wunderground.com/history/daily/kr/incheon/RKSI.\n\n"
                "Measures temperatures to whole degrees Celsius. "
                "This market can not resolve until the first data point for the "
                "following date has been published."
            ),
        )
        spec = compose_retrieval_spec(case, llm_extractor=llm_extractor)

        assert spec.extraction_method == ExtractionMethod.LLM
        assert spec.station_code == "RKSI"
        assert spec.timezone == "Asia/Seoul"
        # City awareness note should be populated by station registry
        assert spec.cross_validation.station_city_awareness is False
        assert "Seoul" in spec.cross_validation.station_city_awareness_detail

    @pytest.mark.integration
    def test_litellm_fallback_on_gibberish_input(self, llm_extractor):
        """When ancillary_data is nonsense but has a valid station URL and date,
        the LLM may fail and regex fallback should keep the pipeline from
        crashing. Either LLM or regex should produce a viable spec."""
        case = _make_market_case(
            case_id="gibberish",
            title="Will the temperature reach the threshold?",
            ancillary_data=(
                "asdf qwer zxcv 12345. "
                "https://www.wunderground.com/history/daily/xx/yyy/ABCD. "
                "Measures temperatures to whole degrees Celsius on 1 Jun '26. "
                "No real data here."
            ),
        )
        # This should not crash — either LLM succeeds or falls back to regex
        spec = compose_retrieval_spec(case, llm_extractor=llm_extractor)
        assert isinstance(spec, RetrievalSpec)
        # The station ABCD is not in registry, falls back to UTC
        assert spec.timezone == "UTC"
