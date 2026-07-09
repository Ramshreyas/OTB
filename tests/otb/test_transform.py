"""Tests for the OTB API → MarketCase transformer."""

from __future__ import annotations

import pytest

from src.otb.api import OTBMarketItem
from src.otb.transform import (
    otb_item_to_market_case,
    otb_items_to_market_cases,
    _derive_case_id,
    _build_polymarket_url,
    _parse_outcomes,
    _normalize_end_date,
    _derive_proposed_outcome,
    _extract_market_id,
)


# ── Sample OTB API item factories ─────────────────────────────────────

def _make_sample_item(**overrides) -> OTBMarketItem:
    """Build a sample OTBMarketItem with sensible defaults."""
    defaults = {
        "question_id": "0x29aed4aeb3736e09189816e4a68c89d1329546fd544b86f0897bf79f1df63eb5",
        "title": "Will the highest temperature in Denver be between 90-91°F on July 8?",
        "ancillary_text": (
            "q: title: Will the highest temperature in Denver be between 90-91°F on July 8?, "
            "description: This market will resolve to the temperature range that contains "
            "the highest temperature recorded at the Buckley Space Force Base Station in "
            "degrees Fahrenheit on 8 Jul '26. Resolution source: "
            "https://www.wunderground.com/history/daily/us/co/aurora/KBKF. "
            "This market can not resolve until the first data point for the following date "
            "has been published. Measures temperatures to whole degrees Fahrenheit. "
            "market_id: 2824904 "
            "res_data: p1: 0, p2: 1, p3: 0.5. "
            "Where p1 corresponds to No, p2 to Yes, p3 to unknown."
        ),
        "end_date": "2026-07-08T00:00:00Z",
        "proposal_time": "2026-07-09T08:15:01Z",
        "status": "settled",
        "market_slug": "highest-temperature-in-denver-on-july-8-2026-90-91f",
        "event_slug": "highest-temperature-in-denver-on-july-8-2026",
        "resolution_conditions": (
            "p1: 0, p2: 1, p3: 0.5. "
            "Where p1 corresponds to No, p2 to Yes, p3 to unknown. "
            "This request MUST only resolve to p1 or p2."
        ),
        "proposed_price": "0",
        "settled_price": "0",
        "proposal_tx_hash": "0x72c6949d9cd85fc933b6095abde2df3881b0c773645b8a9bcaf7a45c68505c62",
        "request_tx_hash": "0x3d5118b57a4aa411821c65eeaad65fb9a2f5e95aedcca94231441f01d4d6cbf1",
        "tags": ("Weather", "Daily Temperature", "Denver"),
        "integrations": ("polymarket",),
        "raw": {},
    }
    defaults.update(overrides)
    return OTBMarketItem(**defaults)


# ── Test: otb_item_to_market_case ──────────────────────────────────────


class TestOTBItemToMarketCase:
    """Tests for the main transformation function."""

    def test_produces_valid_market_case(self):
        """A well-formed OTB item should produce a valid MarketCase."""
        item = _make_sample_item()
        case = otb_item_to_market_case(item)

        assert case.case_id
        assert case.polymarket_url
        assert case.proposal_tx_hash
        assert case.ancillary_data
        assert case.question_data.title == item.title
        assert case.question_data.question_id == item.question_id
        assert case.question_data.end_date_iso

    def test_uses_event_slug_for_polymarket_url(self):
        item = _make_sample_item(event_slug="test-event-slug")
        case = otb_item_to_market_case(item)
        assert "polymarket.com/event/test-event-slug" in case.polymarket_url

    def test_falls_back_to_market_slug_for_polymarket_url(self):
        item = _make_sample_item(event_slug="", market_slug="test-market-slug")
        case = otb_item_to_market_case(item)
        assert "polymarket.com/event/test-market-slug" in case.polymarket_url

    def test_extracts_market_id_from_ancillary(self):
        item = _make_sample_item()
        case = otb_item_to_market_case(item)
        assert case.question_data.market_id == "2824904"

    def test_parses_outcome_labels_correctly(self):
        item = _make_sample_item()
        case = otb_item_to_market_case(item)
        assert case.question_data.outcomes.p1 == "No"
        assert case.question_data.outcomes.p2 == "Yes"
        assert case.question_data.outcomes.p3 == "unknown"
        assert case.question_data.outcomes.p4 == "Too Early"

    def test_derives_proposed_outcome_p1(self):
        item = _make_sample_item(proposed_price="0")
        case = otb_item_to_market_case(item)
        assert case.question_data.proposed_outcome == "p1"

    def test_derives_proposed_outcome_p2(self):
        item = _make_sample_item(proposed_price="1")
        case = otb_item_to_market_case(item)
        assert case.question_data.proposed_outcome == "p2"

    def test_uses_request_tx_hash_as_fallback(self):
        item = _make_sample_item(
            proposal_tx_hash="",
            request_tx_hash="0xabc123",
        )
        case = otb_item_to_market_case(item)
        assert case.proposal_tx_hash == "0xabc123"

    def test_raises_on_empty_ancillary(self):
        """Empty ancillary_data is invalid — MarketCase requires non-empty."""
        import pytest as pt
        item = _make_sample_item(ancillary_text="")
        with pt.raises(ValueError, match="ancillary_data must be a non-empty string"):
            otb_item_to_market_case(item)


# ── Test: otb_items_to_market_cases ────────────────────────────────────


class TestOTBItemsToMarketCases:
    """Tests for the batch transformation function."""

    def test_transforms_multiple_items(self):
        items = tuple(_make_sample_item(
            question_id=f"0x{i:064d}",
            market_slug=f"market-{i}",
        ) for i in range(3))
        cases = otb_items_to_market_cases(items)
        assert len(cases) == 3
        assert all(c.case_id for c in cases)

    def test_status_filter_excludes_items(self):
        items = (
            _make_sample_item(status="settled", question_id="0x1"),
            _make_sample_item(status="proposed", question_id="0x2"),
        )
        cases = otb_items_to_market_cases(items, status_filter="settled")
        assert len(cases) == 1

    def test_skips_invalid_items_and_logs(self):
        """Items that fail transformation should be skipped, not crash."""
        bad_item = _make_sample_item(
            question_id="",  # Will cause empty case_id derivation issue
            market_slug="",
            event_slug="",
        )
        items = (_make_sample_item(), bad_item)
        cases = otb_items_to_market_cases(items)
        assert len(cases) >= 1  # At least the valid one should pass


# ── Test: _derive_case_id ─────────────────────────────────────────────


class TestDeriveCaseId:
    def test_uses_market_slug_with_question_id_prefix(self):
        item = _make_sample_item(
            market_slug="my-market",
            question_id="0xabcdef1234567890",
        )
        cid = _derive_case_id(item)
        assert "my-market" in cid
        assert "0xabcdef" in cid

    def test_falls_back_to_event_slug(self):
        item = _make_sample_item(market_slug="", event_slug="my-event")
        cid = _derive_case_id(item)
        assert "my-event" in cid

    def test_sanitizes_special_characters(self):
        item = _make_sample_item(
            market_slug="market with spaces & special!",
            question_id="0x1234",
        )
        cid = _derive_case_id(item)
        assert " " not in cid
        assert "!" not in cid


# ── Test: _parse_outcomes ──────────────────────────────────────────────


class TestParseOutcomes:
    def test_standard_format(self):
        text = "p1: 0, p2: 1, p3: 0.5. Where p1 corresponds to No, p2 to Yes, p3 to unknown."
        outcomes = _parse_outcomes(text, "Some title")
        assert outcomes.p1 == "No"
        assert outcomes.p2 == "Yes"
        assert outcomes.p3 == "unknown"
        assert outcomes.p4 == "Too Early"

    def test_shared_corresponds_pattern(self):
        """p2 and p3 may not repeat 'corresponds' — just 'p2 to Yes'."""
        text = "Where p1 corresponds to No, p2 to Yes, p3 to unknown."
        outcomes = _parse_outcomes(text, "Some title")
        assert outcomes.p1 == "No"
        assert outcomes.p2 == "Yes"
        assert outcomes.p3 == "unknown"

    def test_defaults_when_not_found(self):
        text = "Simple: p1 is wrong, p2 is right."
        outcomes = _parse_outcomes(text, "Some title")
        assert outcomes.p1 == "No"   # default
        assert outcomes.p2 == "Yes"  # default
        assert outcomes.p3 == "50/50 outcome"


# ── Test: _normalize_end_date ──────────────────────────────────────────


class TestNormalizeEndDate:
    def test_iso_with_z(self):
        assert _normalize_end_date("2026-07-08T00:00:00Z") == "2026-07-08T00:00:00Z"

    def test_iso_without_z(self):
        assert _normalize_end_date("2026-07-08T00:00:00") == "2026-07-08T00:00:00Z"

    def test_date_only(self):
        assert _normalize_end_date("2026-07-08") == "2026-07-08T00:00:00Z"

    def test_empty_string(self):
        assert _normalize_end_date("") == ""

    def test_invalid_passed_through(self):
        assert _normalize_end_date("not-a-date") == "not-a-date"


# ── Test: _derive_proposed_outcome ─────────────────────────────────────


class TestDeriveProposedOutcome:
    def test_zero_is_p1(self):
        assert _derive_proposed_outcome("0") == "p1"

    def test_one_is_p2(self):
        assert _derive_proposed_outcome("1") == "p2"

    def test_half_is_p3(self):
        assert _derive_proposed_outcome("0.5") == "p3"

    def test_invalid_is_none(self):
        assert _derive_proposed_outcome("invalid") is None
        assert _derive_proposed_outcome("") is None


# ── Test: _extract_market_id ───────────────────────────────────────────


class TestExtractMarketId:
    def test_extracts_from_ancillary(self):
        text = "market_id: 2824904 res_data: p1: 0, p2: 1"
        assert _extract_market_id(text, "0x123") == "2824904"

    def test_returns_empty_when_not_found(self):
        assert _extract_market_id("no market id here", "0x123") == ""
