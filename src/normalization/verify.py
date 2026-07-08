"""Window-boundary re-verification (defense-in-depth)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytz


def verify_window(
    observations: tuple[dict[str, Any], ...],
    window_start: datetime,
    window_end: datetime,
    timezone_str: str,
) -> bool:
    """Verify all observations fall within the target window.

    This is defense-in-depth — retrieval already filtered, but normalization
    re-verifies in case of timezone edge cases.

    Args:
        observations: Raw observation dicts with valid_time_gmt (epoch seconds).
        window_start: Window start in station-local time.
        window_end: Window end in station-local time.
        timezone_str: IANA timezone name (e.g., "Asia/Tokyo").

    Returns:
        True if all observations are in-window.
    """
    tz = pytz.timezone(timezone_str)

    # Localize window boundaries to the station timezone for comparison
    window_start_local = tz.localize(window_start) if window_start.tzinfo is None else window_start.astimezone(tz)
    window_end_local = tz.localize(window_end) if window_end.tzinfo is None else window_end.astimezone(tz)

    all_in_window = True

    for obs in observations:
        epoch = obs.get("valid_time_gmt")
        if epoch is None:
            continue

        # Handle both epoch ints and pre-converted datetime objects
        if isinstance(epoch, datetime):
            # Already a datetime — assume it's UTC (from replay module)
            if epoch.tzinfo is None:
                obs_dt = pytz.utc.localize(epoch).astimezone(tz)
            else:
                obs_dt = epoch.astimezone(tz)
        else:
            obs_dt = datetime.fromtimestamp(float(epoch), tz=pytz.utc).astimezone(tz)

        if obs_dt < window_start_local or obs_dt > window_end_local:
            all_in_window = False
            break

    return all_in_window
