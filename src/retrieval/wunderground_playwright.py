"""Playwright Fallback — Headless browser retrieval for Wunderground station pages.

When the Wunderground API fails or returns unusable data, this module uses
Playwright (headless Chromium) to navigate to the station's history page,
toggle the correct unit setting, and scrape the hourly observations table.

Key behaviors:
- Launches a headless Chromium browser
- Navigates to the exact station URL from the RetrievalSpec
- Clicks the gear icon to toggle the unit setting per the spec
- Waits for the observations table to render
- Scrapes all hourly observation rows
- Normalizes scraped fields to match the same shape as the API response
- Records full source trace with path=playwright
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone as dt_timezone
from typing import Any, Optional

from .dispatch import (
    SourceTraceEntry,
    RawObservationBatch,
    ExtractedValue,
    FinalityResult,
    FIELD_MAPPING_TABLE,
)

logger = logging.getLogger(__name__)


# Consent dialog selectors for Sourcepoint CMP used by Wunderground.
# The dialog appears as an iframe (#sp_message_iframe_1225696) with
# Reject All / Accept All buttons inside it.
_CONSENT_IFRAME_ID = "sp_message_iframe_1225696"
_CONSENT_REJECT_SELECTORS = [
    "button.sp_choice_type_13",       # Sourcepoint Reject All (first layer)
    "button[title='Reject All']",
    "button:has-text('Reject All')",
    "button.sp_choice_type_REJECT_ALL",  # GDPR TCF variant
    "button[aria-label='Reject All']",
]


class PlaywrightError(Exception):
    """Raised when Playwright retrieval fails."""

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause


def _get_playwright_browser():
    """Import and launch Playwright browser lazily."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "playwright is required for the Wunderground fallback path. "
            "Install with: pip install playwright && playwright install chromium"
        )

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    return pw, browser


def _dismiss_consent_dialog(page) -> bool:
    """Dismiss the Sourcepoint cookie consent dialog if present.

    Wunderground uses a Sourcepoint CMP iframe overlay
    (#sp_message_iframe_1225696) that blocks pointer events on the
    underlying page. This function attempts to click the Reject All
    button inside the iframe to dismiss it.

    Args:
        page: Playwright Page object after navigation.

    Returns:
        True if the consent dialog was successfully dismissed, False otherwise.
    """
    try:
        # Wait briefly for the consent iframe to appear
        page.wait_for_timeout(2000)

        # Try to locate the consent iframe
        consent_frame = page.frame_locator(f"#{_CONSENT_IFRAME_ID}")
        if not consent_frame:
            logger.debug("No consent iframe found — nothing to dismiss.")
            return False

        # Try each Reject All selector until one clicks successfully
        for selector in _CONSENT_REJECT_SELECTORS:
            try:
                btn = consent_frame.locator(selector).first
                if btn.is_visible(timeout=2000):
                    btn.click(timeout=5000)
                    page.wait_for_timeout(1000)
                    logger.info("Dismissed consent dialog via %s", selector)
                    return True
            except Exception:
                continue

        # Fallback: try the Accept All button as a last resort
        # (better to accept cookies than have the overlay block everything)
        accept_selectors = [
            "button.sp_choice_type_11",  # Sourcepoint Accept All
            "button:has-text('Accept All')",
            "button[title='Accept All']",
            "button.sp_choice_type_ACCEPT_ALL",
        ]
        for selector in accept_selectors:
            try:
                btn = consent_frame.locator(selector).first
                if btn.is_visible(timeout=2000):
                    btn.click(timeout=5000)
                    page.wait_for_timeout(1000)
                    logger.info("Dismissed consent dialog via Accept All: %s", selector)
                    return True
            except Exception:
                continue

        logger.debug("Consent iframe found but no clickable button located.")
        return False

    except Exception as e:
        logger.debug("Consent dialog dismissal skipped: %s", e)
        return False


def _scrape_observation_rows(page) -> list[dict[str, Any]]:
    """Scrape observation rows from the Wunderground history page table.

    Uses JavaScript evaluation to extract the observations table data
    since it's rendered client-side. Falls back to DOM parsing if
    the table is found.

    Args:
        page: Playwright Page object loaded with the station history page.

    Returns:
        List of observation dicts matching the API response shape, each with:
        valid_time_gmt (epoch seconds), temp, wspd, gust, precip_total,
        rh, vis, pressure, dewPt.
    """
    # Wait for the observations table to appear (Angular Material 15+ uses mat-mdc-table)
    page.wait_for_selector(".observation-table, .history-table, .mat-mdc-table, "
                           ".mat-table, table.history", timeout=15000)

    # Extract observations using page.evaluate
    # Wunderground uses Angular Material (mat-mdc-table with cdk-column-* classes).
    # Rows have class mat-mdc-row (v15+) or mat-row (v14 and earlier).
    observations = page.evaluate("""() => {
        const rows = document.querySelectorAll(
            '.mat-mdc-row[role="row"], .mat-row[role="row"], ' +
            'table.history tr, .ng-star-inserted tr[role="row"]'
        );
        const results = [];

        rows.forEach(row => {
            const cells = row.querySelectorAll('td, th');
            if (cells.length < 5) return;  // skip header/empty/footer rows

            const rowData = {};

            cells.forEach(cell => {
                const text = (cell.textContent || '').trim();
                const cls = (cell.className || '');

                // Column detection via cdk-column-* class (Angular Material 15+)
                // or data-column / ng-reflect-name attributes (older versions)
                const colMatch = cls.match(/cdk-column-(\\w+)/);
                const colName = colMatch ? colMatch[1] : '';

                if (colName.includes('date') || colName.includes('time')) {
                    rowData._time_str = text;
                } else if (colName.includes('temp') && !colName.includes('dew')) {
                    const numMatch = text.match(/(-?\\d+\\.?\\d*)/);
                    if (numMatch) rowData.temp = parseFloat(numMatch[1]);
                } else if (colName.includes('dew')) {
                    const numMatch = text.match(/(-?\\d+\\.?\\d*)/);
                    if (numMatch) rowData.dewPt = parseFloat(numMatch[1]);
                } else if (colName.includes('windSpeed') || colName.includes('wspd')) {
                    const numMatch = text.match(/(\\d+\\.?\\d*)/);
                    if (numMatch) rowData.wspd = parseFloat(numMatch[1]);
                } else if (colName.includes('windGust') || colName.includes('gust')) {
                    const numMatch = text.match(/(\\d+\\.?\\d*)/);
                    if (numMatch) rowData.gust = parseFloat(numMatch[1]);
                } else if (colName.includes('precip')) {
                    const numMatch = text.match(/(\\d+\\.?\\d*)/);
                    if (numMatch) rowData.precip_total = parseFloat(numMatch[1]);
                } else if (colName.includes('humid')) {
                    const numMatch = text.match(/(\\d+\\.?\\d*)/);
                    if (numMatch) rowData.rh = parseFloat(numMatch[1]);
                } else if (colName.includes('visib')) {
                    const numMatch = text.match(/(\\d+\\.?\\d*)/);
                    if (numMatch) rowData.vis = parseFloat(numMatch[1]);
                } else if (colName.includes('pressure')) {
                    const numMatch = text.match(/(\\d+\\.?\\d*)/);
                    if (numMatch) rowData.pressure = parseFloat(numMatch[1]);
                }
            });

            if (rowData.temp !== undefined || rowData._time_str) {
                results.push(rowData);
            }
        });

        return results;
    }""")

    return observations


def _toggle_unit_setting(page, unit: str) -> bool:
    """Toggle Wunderground unit between °C and °F via the gear icon.

    Args:
        page: Playwright Page object.
        unit: Target unit ('C' or 'F').

    Returns:
        True if toggle was successful.
    """
    try:
        # Click gear icon
        gear_btn = page.wait_for_selector(
            'button[aria-label="Settings"], mat-icon.settings, '
            '.gear-icon, [data-test="settings"], lib-settings button',
            timeout=5000,
        )
        gear_btn.click()

        # Wait for settings panel
        page.wait_for_timeout(1000)

        # Click the temperature unit toggle
        target_label = "Celsius" if unit == "C" else "Fahrenheit"

        # Try clicking a toggle/switch/radio for temperature unit
        unit_toggle = page.wait_for_selector(
            f'text="{target_label}", mat-slide-toggle:has-text("{target_label}"), '
            f'.unit-toggle:has-text("{target_label}")',
            timeout=3000,
        )
        unit_toggle.click()

        page.wait_for_timeout(1000)
        logger.info("Toggled Wunderground unit to %s", target_label)
        return True

    except Exception as e:
        logger.warning("Could not toggle unit setting: %s. The page may already "
                       "be showing the correct unit.", e)
        return False


def _parse_wunderground_date_from_url(station_url: str, target_date: datetime) -> str:
    """Construct the full dated station URL.

    Args:
        station_url: Base station URL (e.g., .../history/daily/jp/tokyo/RJTT).
        target_date: The target date.

    Returns:
        Full URL with date appended.
    """
    base = station_url.rstrip("/")
    date_str = target_date.strftime("%Y-%m-%d")

    if f"/date/{date_str}" in base:
        return base

    return f"{base}/date/{date_str}"


def _normalize_scraped_to_api_shape(
    scraped: list[dict[str, Any]],
    target_date: datetime,
    timezone_str: str,
) -> list[dict[str, Any]]:
    """Normalize scraped observation rows to match the API response shape.

    Each observation gets:
    - valid_time_gmt: UTC epoch timestamp
    - All standard weather fields
    - obs_time_local: Local time string

    Args:
        scraped: Raw scraped rows from Playwright.
        target_date: The observation date.
        timezone_str: IANA timezone string.

    Returns:
        Normalized observation list matching API response format.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = dt_timezone.utc

    normalized: list[dict[str, Any]] = []

    for row in scraped:
        obs: dict[str, Any] = {
            "temp": row.get("temp"),
            "wspd": row.get("wspd"),
            "gust": row.get("gust"),
            "precip_total": row.get("precip_total"),
            "rh": row.get("rh"),
            "vis": row.get("vis"),
            "pressure": row.get("pressure"),
            "dewPt": row.get("dewPt"),
        }

        # Parse time and convert to UTC epoch
        time_str = row.get("_time_str", "00:00")
        try:
            parts = time_str.replace(":", " ").split()
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0

            local_dt = target_date.replace(
                hour=hour, minute=minute, second=0, microsecond=0,
                tzinfo=tz,
            )
            obs["valid_time_gmt"] = int(local_dt.timestamp())
            obs["obs_time_local"] = local_dt.isoformat()
        except (ValueError, IndexError):
            # Default: midnight UTC
            obs["valid_time_gmt"] = int(target_date.replace(
                tzinfo=dt_timezone.utc).timestamp())
            obs["obs_time_local"] = target_date.isoformat()

        normalized.append(obs)

    return normalized


def fetch_wunderground_playwright(
    station_url: str,
    station_code: str,
    target_window_start: datetime,
    target_window_end: datetime,
    timezone_str: str,
    measurement: str,
    aggregation: str,
    unit: str,
    guardrails: list[str],
) -> RawObservationBatch:
    """Fetch weather observations using Playwright headless browser.

    This is the fallback path when the Wunderground API fails. It navigates
    to the station history page, toggles the unit, scrapes the observations
    table, and normalizes the data to match the API response shape.

    Args:
        station_url: Full Wunderground station URL.
        station_code: ICAO station code.
        target_window_start: Window start.
        target_window_end: Window end.
        timezone_str: IANA timezone string.
        measurement: Measurement type.
        aggregation: Aggregation type.
        unit: Expected unit.
        guardrails: Station-specific guardrails.

    Returns:
        RawObservationBatch with scraped observations.

    Raises:
        PlaywrightError: If Playwright retrieval fails completely.
    """
    trace_entries: list[SourceTraceEntry] = []
    start_time = time.time()

    # Build the dated URL
    page_url = _parse_wunderground_date_from_url(station_url, target_window_start)

    logger.info("Playwright fallback: navigating to %s", page_url)

    pw = None
    browser = None
    observations: list[dict[str, Any]] = []

    try:
        pw, browser = _get_playwright_browser()
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Navigate to the station page
        nav_start = time.time()
        page.goto(page_url, wait_until="networkidle", timeout=30000)
        nav_ms = (time.time() - nav_start) * 1000

        # Dismiss cookie consent popup before interacting with the page.
        # Wunderground uses Sourcepoint CMP which blocks pointer events
        # via an iframe overlay if not dismissed.
        _dismiss_consent_dialog(page)

        # Toggle unit if needed
        _toggle_unit_setting(page, unit)

        # Wait a bit for re-render after unit toggle
        page.wait_for_timeout(2000)

        # Scrape observations
        scraped = _scrape_observation_rows(page)

        # Normalize to API response shape
        observations = _normalize_scraped_to_api_shape(
            scraped, target_window_start, timezone_str,
        )

        elapsed_ms = (time.time() - start_time) * 1000

        trace_entries.append(SourceTraceEntry(
            url=page_url,
            http_status=200,
            response_size_bytes=len(str(observations)),
            latency_ms=elapsed_ms,
            path="playwright",
            retry_count=0,
            guardrail_flags=[],
            error=None,
            timestamp=datetime.utcnow().isoformat(),
        ))

        logger.info(
            "Playwright scraped %d observation rows from %s",
            len(observations), station_code,
        )

    except Exception as e:
        elapsed_ms = (time.time() - start_time) * 1000
        trace_entries.append(SourceTraceEntry(
            url=page_url,
            http_status=None,
            response_size_bytes=0,
            latency_ms=elapsed_ms,
            path="playwright",
            retry_count=0,
            guardrail_flags=[],
            error=f"Playwright failed: {e}",
            timestamp=datetime.utcnow().isoformat(),
        ))
        raise PlaywrightError(
            f"Playwright retrieval failed for {station_code}: {e}",
            cause=e,
        ) from e

    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()

    # Apply aggregation
    field_mapping = FIELD_MAPPING_TABLE.get((measurement, aggregation))
    api_field = field_mapping.api_field if field_mapping else "temp"
    agg_fn_name = aggregation

    values: list[float] = []
    for obs in observations:
        val = obs.get(api_field)
        if val is not None:
            try:
                values.append(float(val))
            except (ValueError, TypeError):
                continue

    if aggregation == "min":
        extracted_val = min(values) if values else None
    elif aggregation == "max":
        extracted_val = max(values) if values else None
    elif aggregation == "sum":
        extracted_val = sum(values) if values else None
    else:
        extracted_val = values[0] if values else None

    extracted_value = ExtractedValue(
        value=extracted_val,
        unit=unit,
        field=api_field,
        aggregation=aggregation,
    )

    # Finality: Playwright path doesn't check finality separately
    # We assume not_yet unless we can verify
    finality = FinalityResult(status="not_yet", first_next_day_ts=None)

    return RawObservationBatch(
        observations=tuple(observations),
        extracted_value=extracted_value,
        finality=finality,
        source_trace=tuple(trace_entries),
    )
