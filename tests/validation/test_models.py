"""Tests for the market case data models (immutability)."""

from __future__ import annotations

import pytest

from src.validation.models import MarketCase, MarketManifest, Outcomes, QuestionData


# ═══════════════════════════════════════════════════════════════════════
# Outcomes
# ═══════════════════════════════════════════════════════════════════════


class TestOutcomes:
    """Tests for the Outcomes frozen dataclass."""

    def test_creation(self):
        """Should create an Outcomes instance with all four labels."""
        o = Outcomes(p1="No", p2="Yes", p3="50/50 outcome", p4="Too Early")
        assert o.p1 == "No"
        assert o.p2 == "Yes"
        assert o.p3 == "50/50 outcome"
        assert o.p4 == "Too Early"

    def test_is_frozen(self):
        """Outcomes should be immutable."""
        o = Outcomes(p1="No", p2="Yes", p3="50/50 outcome", p4="Too Early")
        with pytest.raises(Exception):  # FrozenInstanceError (dataclasses) or AttributeError
            o.p1 = "Changed"  # type: ignore[misc]

    def test_equality(self):
        """Two Outcomes with the same values should be equal."""
        o1 = Outcomes(p1="No", p2="Yes", p3="50/50", p4="Too Early")
        o2 = Outcomes(p1="No", p2="Yes", p3="50/50", p4="Too Early")
        assert o1 == o2

    def test_inequality(self):
        """Two Outcomes with different values should not be equal."""
        o1 = Outcomes(p1="No", p2="Yes", p3="50/50", p4="Too Early")
        o2 = Outcomes(p1="No", p2="Yes", p3="50/50", p4="Not Too Early")
        assert o1 != o2


# ═══════════════════════════════════════════════════════════════════════
# QuestionData
# ═══════════════════════════════════════════════════════════════════════


class TestQuestionData:
    """Tests for the QuestionData frozen dataclass."""

    @pytest.fixture
    def sample_outcomes(self) -> Outcomes:
        return Outcomes(p1="No", p2="Yes", p3="50/50 outcome", p4="Too Early")

    def test_creation_minimal(self, sample_outcomes):
        """Should create with only required fields."""
        qd = QuestionData(
            title="Will the temperature be 20°C?",
            end_date_iso="2026-06-01",
            outcomes=sample_outcomes,
        )
        assert qd.title == "Will the temperature be 20°C?"
        assert qd.end_date_iso == "2026-06-01"
        assert qd.outcomes == sample_outcomes
        assert qd.question_id is None
        assert qd.market_id is None

    def test_creation_full(self, sample_outcomes):
        """Should create with all fields populated."""
        qd = QuestionData(
            title="Will the temperature be 20°C?",
            end_date_iso="2026-06-01",
            outcomes=sample_outcomes,
            question_id="0xabc",
            market_id="123",
            market_slug="slug",
            gamma_slug="gamma",
            proposal_time="2026-06-01T15:00:00Z",
            processed_time="2026-06-01T15:01:00Z",
            resolution_conditions="p1: No. p2: Yes.",
            proposed_outcome="p2",
        )
        assert qd.question_id == "0xabc"
        assert qd.market_id == "123"
        assert qd.proposed_outcome == "p2"

    def test_is_frozen(self, sample_outcomes):
        """QuestionData should be immutable."""
        qd = QuestionData(
            title="Will the temperature be 20°C?",
            end_date_iso="2026-06-01",
            outcomes=sample_outcomes,
        )
        with pytest.raises(Exception):
            qd.title = "Changed"  # type: ignore[misc]

    def test_field_access_by_name(self, sample_outcomes):
        """Fields should be accessible by attribute name."""
        qd = QuestionData(
            title="Test title",
            end_date_iso="2026-06-01",
            outcomes=sample_outcomes,
        )
        # Regular attribute access
        _ = qd.title
        _ = qd.end_date_iso
        _ = qd.outcomes


# ═══════════════════════════════════════════════════════════════════════
# MarketCase
# ═══════════════════════════════════════════════════════════════════════


class TestMarketCase:
    """Tests for the MarketCase frozen dataclass."""

    @pytest.fixture
    def sample_question_data(self) -> QuestionData:
        return QuestionData(
            title="Will the lowest temperature in Tokyo be 20°C on June 1?",
            end_date_iso="2026-06-01T00:00:00Z",
            outcomes=Outcomes(p1="No", p2="Yes", p3="50/50 outcome", p4="Too Early"),
            question_id="0xcb8828",
            market_id="2391835",
            market_slug="lowest-temperature-in-tokyo",
            gamma_slug="lowest-temperature-in-tokyo-gamma",
            proposal_time="2026-06-01T15:06:18Z",
            processed_time="2026-06-01T15:07:44Z",
            resolution_conditions="p1: No. p2: Yes. p3: 50/50. p4: Too Early.",
            proposed_outcome="p2",
        )

    @pytest.fixture
    def sample_ancillary_data(self) -> str:
        return (
            "q: title: Will the lowest temperature in Tokyo be 20°C on June 1?, "
            "description: Resolution source: "
            "https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
            "Measures temperatures to whole degrees Celsius. "
            "res_data: p1: 0, p2: 1, p3: 0.5."
        )

    def test_creation(self, sample_question_data, sample_ancillary_data):
        """Should create a MarketCase with all required fields."""
        case = MarketCase(
            case_id="tokyo_low_2026_06_01_20c",
            polymarket_url="https://polymarket.com/event/tokyo-low",
            proposal_tx_hash="0x59460aab91d57412308e194816f24939726baa831990fa225d64ef1e23689b18",
            question_data=sample_question_data,
            ancillary_data=sample_ancillary_data,
        )
        assert case.case_id == "tokyo_low_2026_06_01_20c"
        assert case.polymarket_url == "https://polymarket.com/event/tokyo-low"
        assert case.question_data == sample_question_data
        assert case.ancillary_data == sample_ancillary_data

    def test_creation_with_fixture_path(self, sample_question_data, sample_ancillary_data):
        """Should accept an optional fixture_path."""
        case = MarketCase(
            case_id="test",
            polymarket_url="https://polymarket.com/event/test",
            proposal_tx_hash="0x" + "a" * 64,
            question_data=sample_question_data,
            ancillary_data=sample_ancillary_data,
            fixture_path="data/fixtures/test_fixture.json",
        )
        assert case.fixture_path == "data/fixtures/test_fixture.json"

    def test_is_frozen(self, sample_question_data, sample_ancillary_data):
        """MarketCase should be immutable."""
        case = MarketCase(
            case_id="test",
            polymarket_url="https://polymarket.com/event/test",
            proposal_tx_hash="0x" + "a" * 64,
            question_data=sample_question_data,
            ancillary_data=sample_ancillary_data,
        )
        with pytest.raises(Exception):
            case.case_id = "changed"  # type: ignore[misc]

    def test_nested_immutability(self, sample_question_data, sample_ancillary_data):
        """Nested frozen dataclasses (question_data, outcomes) should also be immutable."""
        case = MarketCase(
            case_id="test",
            polymarket_url="https://polymarket.com/event/test",
            proposal_tx_hash="0x" + "a" * 64,
            question_data=sample_question_data,
            ancillary_data=sample_ancillary_data,
        )
        # Can't mutate question_data
        with pytest.raises(Exception):
            case.question_data.title = "changed"  # type: ignore[misc]

        # Can't mutate outcomes through question_data
        with pytest.raises(Exception):
            case.question_data.outcomes.p1 = "changed"  # type: ignore[misc]

    def test_empty_case_id_rejected(self, sample_question_data, sample_ancillary_data):
        """Empty case_id should raise ValueError in __post_init__."""
        with pytest.raises(ValueError, match="case_id"):
            MarketCase(
                case_id="",
                polymarket_url="https://polymarket.com/event/test",
                proposal_tx_hash="0x" + "a" * 64,
                question_data=sample_question_data,
                ancillary_data=sample_ancillary_data,
            )

    def test_whitespace_case_id_rejected(self, sample_question_data, sample_ancillary_data):
        """Whitespace-only case_id should raise ValueError."""
        with pytest.raises(ValueError, match="case_id"):
            MarketCase(
                case_id="   ",
                polymarket_url="https://polymarket.com/event/test",
                proposal_tx_hash="0x" + "a" * 64,
                question_data=sample_question_data,
                ancillary_data=sample_ancillary_data,
            )

    def test_empty_ancillary_data_rejected(self, sample_question_data):
        """Empty ancillary_data should raise ValueError in __post_init__."""
        with pytest.raises(ValueError, match="ancillary_data"):
            MarketCase(
                case_id="test",
                polymarket_url="https://polymarket.com/event/test",
                proposal_tx_hash="0x" + "a" * 64,
                question_data=sample_question_data,
                ancillary_data="",
            )

    def test_equality(self, sample_question_data, sample_ancillary_data):
        """Two identical MarketCases should be equal."""
        kwargs = {
            "case_id": "test",
            "polymarket_url": "https://polymarket.com/event/test",
            "proposal_tx_hash": "0x" + "a" * 64,
            "question_data": sample_question_data,
            "ancillary_data": sample_ancillary_data,
        }
        c1 = MarketCase(**kwargs)
        c2 = MarketCase(**kwargs)
        assert c1 == c2

    def test_inequality(self, sample_question_data, sample_ancillary_data):
        """MarketCases with different case_ids should not be equal."""
        kwargs = {
            "polymarket_url": "https://polymarket.com/event/test",
            "proposal_tx_hash": "0x" + "a" * 64,
            "question_data": sample_question_data,
            "ancillary_data": sample_ancillary_data,
        }
        c1 = MarketCase(case_id="case_a", **kwargs)
        c2 = MarketCase(case_id="case_b", **kwargs)
        assert c1 != c2


# ═══════════════════════════════════════════════════════════════════════
# MarketManifest
# ═══════════════════════════════════════════════════════════════════════


class TestMarketManifest:
    """Tests for the MarketManifest frozen dataclass."""

    @pytest.fixture
    def sample_case(self) -> MarketCase:
        return MarketCase(
            case_id="test",
            polymarket_url="https://polymarket.com/event/test",
            proposal_tx_hash="0x" + "a" * 64,
            question_data=QuestionData(
                title="Test?",
                end_date_iso="2026-06-01",
                outcomes=Outcomes(p1="No", p2="Yes", p3="50/50", p4="Too Early"),
            ),
            ancillary_data=(
                "Source: https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
                "res_data: p1: 0, p2: 1, p3: 0.5."
            ),
        )

    def test_creation_empty(self):
        """Should create with an empty markets tuple."""
        manifest = MarketManifest(schema_version="otb-weather-case-v1")
        assert manifest.schema_version == "otb-weather-case-v1"
        assert manifest.markets == ()
        assert manifest.description is None

    def test_creation_with_markets(self, sample_case):
        """Should create with a tuple of MarketCases."""
        manifest = MarketManifest(
            schema_version="otb-weather-case-v1",
            markets=(sample_case,),
            description="Test manifest",
        )
        assert len(manifest.markets) == 1
        assert manifest.markets[0] == sample_case
        assert manifest.description == "Test manifest"

    def test_markets_is_tuple(self, sample_case):
        """markets should be stored as an immutable tuple."""
        manifest = MarketManifest(
            schema_version="otb-weather-case-v1",
            markets=(sample_case,),
        )
        assert isinstance(manifest.markets, tuple)

    def test_is_frozen(self, sample_case):
        """MarketManifest should be immutable."""
        manifest = MarketManifest(
            schema_version="otb-weather-case-v1",
            markets=(sample_case,),
        )
        with pytest.raises(Exception):
            manifest.schema_version = "changed"  # type: ignore[misc]

    def test_market_selection_optional(self, sample_case):
        """market_selection is optional and accepts arbitrary dicts."""
        manifest = MarketManifest(
            schema_version="otb-weather-case-v1",
            markets=(sample_case,),
            market_selection={"project": "OTB", "notes": "test"},
        )
        assert manifest.market_selection == {"project": "OTB", "notes": "test"}
