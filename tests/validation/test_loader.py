"""Tests for the market manifest loader.

Covers:
- Loading the real markets.json
- Loading valid manifests end-to-end
- Error handling for missing files, invalid JSON, schema violations
- All five real market cases from markets.json are loaded correctly
- Field-level correctness for the Tokyo low case
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.validation.loader import LoadError, load_markets
from src.validation.models import MarketCase, MarketManifest
from tests.conftest import make_case_with_overrides, make_valid_manifest


# ═══════════════════════════════════════════════════════════════════════
# Real markets.json loading
# ═══════════════════════════════════════════════════════════════════════


class TestRealMarketsJson:
    """Tests that load the actual data/markets.json file."""

    def test_loads_all_five_cases(self, markets_json_path):
        """Should load all 5 market cases from the real markets.json."""
        manifest = load_markets(markets_json_path)

        assert isinstance(manifest, MarketManifest)
        assert len(manifest.markets) == 5

    def test_schema_version_is_present(self, markets_json_path):
        """The schema_version from markets.json should be loaded."""
        manifest = load_markets(markets_json_path)

        assert manifest.schema_version == "otb-weather-case-v1"

    def test_all_case_ids_are_unique(self, markets_json_path):
        """All case_ids in the real manifest should be unique."""
        manifest = load_markets(markets_json_path)

        case_ids = [c.case_id for c in manifest.markets]
        assert len(case_ids) == len(set(case_ids))

    @pytest.mark.parametrize("expected_case_id", [
        "tokyo_low_2026_06_01_20c",
        "tokyo_high_2026_06_01_29c_or_higher",
        "busan_high_2026_06_01_22c_or_below",
        "seoul_low_2026_06_01_16c",
        "denver_high_2026_05_31_68_69f",
    ])
    def test_expected_case_ids_present(self, markets_json_path, expected_case_id):
        """Each expected case_id should be present in the loaded manifest."""
        manifest = load_markets(markets_json_path)

        case_ids = [c.case_id for c in manifest.markets]
        assert expected_case_id in case_ids

    def test_tokyo_low_case_fields(self, markets_json_path):
        """The Tokyo low case should be loaded with correct field values."""
        manifest = load_markets(markets_json_path)

        tokyo = next(
            c for c in manifest.markets
            if c.case_id == "tokyo_low_2026_06_01_20c"
        )

        # Top-level fields
        assert tokyo.polymarket_url == (
            "https://polymarket.com/event/lowest-temperature-in-tokyo-on-june-1-2026"
        )
        assert tokyo.proposal_tx_hash == (
            "0x59460aab91d57412308e194816f24939726baa831990fa225d64ef1e23689b18"
        )

        # Question data
        assert tokyo.question_data.title == (
            "Will the lowest temperature in Tokyo be 20°C on June 1?"
        )
        assert tokyo.question_data.end_date_iso == "2026-06-01T00:00:00Z"
        assert tokyo.question_data.market_id == "2391835"

        # Outcomes
        assert tokyo.question_data.outcomes.p1 == "No"
        assert tokyo.question_data.outcomes.p2 == "Yes"
        assert tokyo.question_data.outcomes.p3 == "50/50 outcome"
        assert tokyo.question_data.outcomes.p4 == "Too Early"

        # Ancillary data contains station URL
        assert "wunderground.com/history/daily/jp/tokyo/RJTT" in tokyo.ancillary_data

    def test_denver_case_uses_buckley_station(self, markets_json_path):
        """The Denver case should reference KBKF (Buckley SFB), not a Denver city station."""
        manifest = load_markets(markets_json_path)

        denver = next(
            c for c in manifest.markets
            if c.case_id == "denver_high_2026_05_31_68_69f"
        )

        assert "KBKF" in denver.ancillary_data
        assert "Buckley" in denver.ancillary_data
        assert "aurora" in denver.ancillary_data  # lowercase in URL path

    def test_seoul_case_uses_incheon_station(self, markets_json_path):
        """The Seoul case should reference RKSI (Incheon), not a Seoul city station."""
        manifest = load_markets(markets_json_path)

        seoul = next(
            c for c in manifest.markets
            if c.case_id == "seoul_low_2026_06_01_16c"
        )

        assert "RKSI" in seoul.ancillary_data
        assert "Incheon" in seoul.ancillary_data

    def test_busan_case_uses_gimhae_station(self, markets_json_path):
        """The Busan case should reference RKPK (Gimhae), not a Busan city station."""
        manifest = load_markets(markets_json_path)

        busan = next(
            c for c in manifest.markets
            if c.case_id == "busan_high_2026_06_01_22c_or_below"
        )

        assert "RKPK" in busan.ancillary_data
        assert "Gimhae" in busan.ancillary_data

    def test_all_cases_are_immutable(self, markets_json_path):
        """Every loaded MarketCase should be immutable."""
        manifest = load_markets(markets_json_path)

        for case in manifest.markets:
            with pytest.raises(Exception):
                case.case_id = "mutated"  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
# Loader — valid manifests
# ═══════════════════════════════════════════════════════════════════════


class TestLoaderValid:
    """End-to-end loading of valid manifests through a temp file."""

    def test_loads_valid_manifest_from_file(self, tmp_path, valid_manifest):
        """A valid manifest written to a temp file should load successfully."""
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(valid_manifest), encoding="utf-8")

        manifest = load_markets(filepath)

        assert isinstance(manifest, MarketManifest)
        assert len(manifest.markets) == 1
        assert manifest.markets[0].case_id == "test_case_01"

    def test_loads_five_cases_from_file(self, tmp_path, valid_manifest_five_cases):
        """A manifest with five cases should load all of them."""
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(valid_manifest_five_cases), encoding="utf-8")

        manifest = load_markets(filepath)

        assert len(manifest.markets) == 5
        case_ids = [c.case_id for c in manifest.markets]
        assert case_ids == ["case_01", "case_02", "case_03", "case_04", "case_05"]

    def test_all_loaded_cases_are_marketcase_instances(self, tmp_path, valid_manifest):
        """Every loaded market should be a MarketCase instance."""
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(valid_manifest), encoding="utf-8")

        manifest = load_markets(filepath)

        for case in manifest.markets:
            assert isinstance(case, MarketCase)

    def test_manifest_metadata_is_preserved(self, tmp_path, valid_manifest):
        """Optional manifest metadata (description, generated_at) should be loaded."""
        valid_manifest["description"] = "Custom description"
        valid_manifest["generated_at"] = "2026-07-08T12:00:00Z"
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(valid_manifest), encoding="utf-8")

        manifest = load_markets(filepath)

        assert manifest.description == "Custom description"
        assert manifest.generated_at == "2026-07-08T12:00:00Z"


# ═══════════════════════════════════════════════════════════════════════
# Loader — error cases
# ═══════════════════════════════════════════════════════════════════════


class TestLoaderErrors:
    """Loader should raise LoadError for various invalid inputs."""

    def test_missing_file_raises(self, tmp_path):
        """Loading a nonexistent file should raise LoadError."""
        filepath = tmp_path / "does_not_exist.json"

        with pytest.raises(LoadError, match="not found"):
            load_markets(filepath)

    def test_invalid_json_raises(self, tmp_path):
        """Loading a file with invalid JSON should raise LoadError."""
        filepath = tmp_path / "bad.json"
        filepath.write_text("{ not valid json }", encoding="utf-8")

        with pytest.raises(LoadError, match="Failed to parse"):
            load_markets(filepath)

    def test_non_dict_json_raises(self, tmp_path):
        """Loading a JSON array (not object) at top level should raise LoadError."""
        filepath = tmp_path / "array.json"
        filepath.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        with pytest.raises(LoadError, match="JSON object"):
            load_markets(filepath)

    def test_schema_violation_raises(self, tmp_path):
        """A manifest with a schema violation should raise LoadError."""
        manifest = make_valid_manifest()
        del manifest["markets"][0]["case_id"]  # Make it invalid
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(LoadError, match="Schema validation failed"):
            load_markets(filepath)

    def test_duplicate_case_ids_raises(self, tmp_path):
        """A manifest with duplicate case_ids should raise LoadError."""
        manifest = make_valid_manifest(
            markets=[
                make_case_with_overrides(case_id="dup_id"),
                make_case_with_overrides(case_id="dup_id"),
            ]
        )
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(LoadError, match="Duplicate case_id"):
            load_markets(filepath)

    def test_unparseable_date_raises(self, tmp_path):
        """A manifest with an unparseable end_date_iso should raise LoadError."""
        case = make_case_with_overrides(
            case_id="bad_date",
            question_data__end_date_iso="not-a-date",
        )
        manifest = make_valid_manifest(markets=[case])
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(LoadError, match="Date validation failed"):
            load_markets(filepath)

    def test_missing_station_url_raises(self, tmp_path):
        """A manifest with no station URL in ancillary_data should raise LoadError."""
        case = make_case_with_overrides(
            ancillary_data="No URL here, just some text about weather."
        )
        manifest = make_valid_manifest(markets=[case])
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(LoadError, match="Station URL validation failed"):
            load_markets(filepath)

    def test_empty_markets_array_raises(self, tmp_path):
        """A manifest with an empty markets array should raise LoadError."""
        manifest = make_valid_manifest(markets=[])
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(LoadError, match="Schema validation"):
            load_markets(filepath)


# ═══════════════════════════════════════════════════════════════════════
# Loader — edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestLoaderEdgeCases:
    """Edge cases for the loader."""

    def test_fixture_path_is_loaded_when_present(self, tmp_path):
        """The optional fixture_path field should be propagated to MarketCase."""
        case = make_case_with_overrides(fixture_path="data/fixtures/custom.json")
        manifest = make_valid_manifest(markets=[case])
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(manifest), encoding="utf-8")

        result = load_markets(filepath)

        assert result.markets[0].fixture_path == "data/fixtures/custom.json"

    def test_fixture_path_is_none_when_absent(self, tmp_path, valid_manifest):
        """When fixture_path is not in the JSON, it should default to None."""
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(valid_manifest), encoding="utf-8")

        result = load_markets(filepath)

        assert result.markets[0].fixture_path is None

    def test_markets_preserve_input_order(self, tmp_path):
        """Markets should be returned in the same order as the input array."""
        cases = [
            make_case_with_overrides(case_id=f"case_{chr(65 + i)}")
            for i in range(5)
        ]
        manifest = make_valid_manifest(markets=cases)
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(manifest), encoding="utf-8")

        result = load_markets(filepath)

        loaded_ids = [c.case_id for c in result.markets]
        assert loaded_ids == ["case_A", "case_B", "case_C", "case_D", "case_E"]

    def test_market_selection_metadata_is_preserved(self, tmp_path):
        """The market_selection metadata block should be preserved."""
        manifest = make_valid_manifest()
        manifest["market_selection"] = {
            "project": "OTB Polymarket weather markets",
            "selection_notes": "Recent Wunderground-backed markets",
        }
        filepath = tmp_path / "markets.json"
        filepath.write_text(json.dumps(manifest), encoding="utf-8")

        result = load_markets(filepath)

        assert result.market_selection is not None
        assert result.market_selection["project"] == "OTB Polymarket weather markets"
