"""OTB Oracle API client — fetches Weather markets from the OTB API.

Observability: every API call is traced (Langfuse span) and every response is
logged with structured metadata for debugging and replay.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# ── Default API endpoint ──────────────────────────────────────────────

OTB_API_BASE = "https://oracle.api.otb.uma.xyz"
DEFAULT_PAGE_SIZE = 50


# ── Data models ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class OTBMarketItem:
    """A single market item from the OTB Oracle API response.

    This is the raw item, not yet transformed into a MarketCase.
    All fields come directly from the API.

    Attributes:
        question_id: Unique OTB question identifier.
        title: Market question title.
        ancillary_text: Full ancillary data string (station URL, rules, etc.).
        end_date: ISO 8601 end date for the market.
        proposal_time: ISO 8601 proposal timestamp.
        status: Market status (proposed, settled, disputed).
        market_slug: URL-friendly market slug.
        event_slug: Parent event slug (for Polymarket URL reconstruction).
        resolution_conditions: Text describing resolution conditions.
        proposed_price: On-chain proposed price.
        settled_price: On-chain settled price (if settled).
        proposal_tx_hash: Proposal transaction hash.
        request_tx_hash: Request transaction hash.
        tags: List of market tags.
        integrations: List of visible integrations.
        raw: The complete raw API item dict (for trace/debug).
    """

    question_id: str
    title: str
    ancillary_text: str
    end_date: str
    proposal_time: str
    status: str
    market_slug: str
    event_slug: str
    resolution_conditions: str
    proposed_price: str
    settled_price: str
    proposal_tx_hash: str
    request_tx_hash: str
    tags: tuple[str, ...]
    integrations: tuple[str, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class OTBFetchResult:
    """Result of a single page fetch from the OTB Oracle API.

    Attributes:
        items: List of OTBMarketItem objects from this page.
        page: Page number fetched.
        page_size: Number of items per page.
        total: Total count of matching items across all pages.
        total_pages: Total number of pages available.
        fetched_at: UTC timestamp of when the fetch completed.
        latency_ms: Round-trip latency of the HTTP request.
        http_status: HTTP status code.
    """

    items: tuple[OTBMarketItem, ...]
    page: int
    page_size: int
    total: int
    total_pages: int
    fetched_at: str
    latency_ms: float
    http_status: int


# ── Exceptions ─────────────────────────────────────────────────────────


class OTBAPIError(Exception):
    """Raised when the OTB Oracle API request fails.

    Attributes:
        message: Human-readable error description.
        http_status: HTTP status code, or None if not an HTTP error.
        response_body: Truncated response body for debugging.
    """

    def __init__(
        self,
        message: str,
        http_status: Optional[int] = None,
        response_body: str = "",
    ):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.response_body = response_body[:1000]


# ── Client ─────────────────────────────────────────────────────────────


class OTBClient:
    """Client for the OTB Oracle API.

    Handles pagination, observability, and raw payload persistence.

    Usage:
        client = OTBClient()
        result = client.fetch_weather_markets(status="proposed", max_items=50)
        for item in result.items:
            ...
    """

    def __init__(
        self,
        base_url: str = OTB_API_BASE,
        timeout: float = 30.0,
        retries: int = 3,
        retry_backoff: float = 2.0,
    ):
        """Initialize the OTB API client.

        Args:
            base_url: Base URL for the OTB Oracle API.
            timeout: HTTP request timeout in seconds.
            retries: Number of retries on transient failures.
            retry_backoff: Multiplier for exponential backoff between retries.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._retries = retries
        self._retry_backoff = retry_backoff
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "OTB-Weather-Resolver/0.1",
        })

    # ── Public API ─────────────────────────────────────────────────

    def fetch_weather_markets(
        self,
        *,
        status: str = "proposed",
        integrations: str = "polymarket,predict-fun",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_items: Optional[int] = None,
        persist_raw_dir: Optional[str | Path] = None,
    ) -> OTBFetchResult:
        """Fetch Weather markets from the OTB Oracle API.

        Args:
            status: Market status filter. "proposed" for open markets,
                "settled" for resolved, or empty for all.
            integrations: Comma-separated integration names.
            page: Page number to fetch.
            page_size: Items per page (1-100).
            max_items: If set, stop fetching after this many items
                (across pages). None = fetch only this page.
            persist_raw_dir: If set, persist the raw API response as JSON
                in this directory for observability and replay.

        Returns:
            OTBFetchResult with fetched items and pagination metadata.

        Raises:
            OTBAPIError: If the API request fails after all retries.
        """
        items: list[OTBMarketItem] = []
        current_page = page
        total_fetched = 0
        total_available = 0
        total_pages_available = 0
        all_latency_ms = 0.0
        last_http_status = 0
        fetched_at = datetime.now(timezone.utc).isoformat()

        while True:
            url = self._build_url(
                status=status,
                integrations=integrations,
                page=current_page,
                page_size=page_size,
            )

            logger.info("Fetching OTB markets: page=%d page_size=%d status=%s",
                        current_page, page_size, status)

            # ── Trace the API call ──
            start = time.monotonic()
            try:
                raw_response = self._fetch_with_retry(url)
            except Exception as e:
                raise OTBAPIError(
                    f"Failed to fetch OTB API after {self._retries} retries: {e}",
                ) from e
            elapsed_ms = (time.monotonic() - start) * 1000
            all_latency_ms += elapsed_ms

            data = raw_response
            last_http_status = 200

            # Persist raw payload
            if persist_raw_dir:
                self._persist_raw(data, current_page, persist_raw_dir)

            # Parse items
            raw_items = data.get("items", [])
            total_available = data.get("total", 0)
            total_pages_available = data.get("total_pages", 0)

            page_items = [_parse_item(item) for item in raw_items]
            items.extend(page_items)
            total_fetched += len(page_items)

            logger.info(
                "OTB API page %d/%d: %d items fetched (total so far: %d, "
                "available: %d, %.0fms)",
                current_page, total_pages_available,
                len(page_items), total_fetched, total_available, elapsed_ms,
            )

            # Stop conditions
            if max_items is not None and total_fetched >= max_items:
                items = items[:max_items]
                break
            if current_page >= total_pages_available:
                break
            if len(raw_items) == 0:
                break

            current_page += 1

        # Trace via Langfuse
        self._trace_fetch(
            url=self._build_url(status=status, integrations=integrations,
                                page=page, page_size=page_size),
            status_filter=status,
            page=page,
            page_size=page_size,
            items_fetched=len(items),
            total_available=total_available,
            latency_ms=all_latency_ms,
            http_status=last_http_status,
        )

        return OTBFetchResult(
            items=tuple(items),
            page=page,
            page_size=page_size,
            total=total_available,
            total_pages=total_pages_available,
            fetched_at=fetched_at,
            latency_ms=all_latency_ms,
            http_status=last_http_status,
        )

    # ── Internals ──────────────────────────────────────────────────

    def _build_url(
        self,
        *,
        status: str,
        integrations: str,
        page: int,
        page_size: int,
    ) -> str:
        """Build the OTB API URL with query parameters."""
        params = {
            "tags_any": "Weather",
            "visible_integrations": integrations,
            "date_field": "proposal_block_time",
            "sort_by": "proposal_time",
            "sort_order": "desc",
            "page": str(page),
            "page_size": str(page_size),
        }
        if status:
            params["status"] = status

        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self._base_url}/requests?{qs}"

    def _fetch_with_retry(self, url: str) -> dict[str, Any]:
        """Fetch a URL with retry logic.

        Args:
            url: Full API URL.

        Returns:
            Parsed JSON response dict.

        Raises:
            OTBAPIError: If all retries are exhausted.
        """
        last_error: Optional[Exception] = None
        last_status: Optional[int] = None
        last_body: str = ""

        for attempt in range(self._retries + 1):
            try:
                response = self._session.get(url, timeout=self._timeout)
                last_status = response.status_code

                if response.status_code == 200:
                    return response.json()

                last_body = response.text[:500]
                if response.status_code in (429, 503, 502):
                    # Transient — retry with backoff
                    wait = self._retry_backoff ** attempt
                    logger.warning(
                        "OTB API returned %d (attempt %d/%d), retrying in %.1fs",
                        response.status_code, attempt + 1,
                        self._retries + 1, wait,
                    )
                    time.sleep(wait)
                    continue

                # Non-transient error
                raise OTBAPIError(
                    f"OTB API returned HTTP {response.status_code}",
                    http_status=response.status_code,
                    response_body=last_body,
                )

            except requests.RequestException as e:
                last_error = e
                if attempt < self._retries:
                    wait = self._retry_backoff ** attempt
                    logger.warning(
                        "OTB API request failed (attempt %d/%d): %s. "
                        "Retrying in %.1fs",
                        attempt + 1, self._retries + 1, e, wait,
                    )
                    time.sleep(wait)
                else:
                    raise OTBAPIError(
                        f"OTB API request failed after {self._retries + 1} "
                        f"attempts: {e}",
                        http_status=last_status,
                        response_body=last_body,
                    ) from e

        # Shouldn't reach here
        raise OTBAPIError(
            f"OTB API exhausted all retries",
            http_status=last_status,
            response_body=last_body,
        )

    def _persist_raw(
        self,
        data: dict[str, Any],
        page: int,
        directory: str | Path,
    ) -> None:
        """Persist raw API response for observability and replay.

        Saved as {directory}/otb_page_{page}_{timestamp}.json
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        filename = f"otb_page_{page:03d}_{ts}.json"
        path = directory / filename

        # Strip items to keep the payload manageable; items are stored
        # separately in individual case payloads.
        payload = {k: v for k, v in data.items()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)

        logger.debug("Persisted raw OTB API page to %s", path)

    def _trace_fetch(
        self,
        *,
        url: str,
        status_filter: str,
        page: int,
        page_size: int,
        items_fetched: int,
        total_available: int,
        latency_ms: float,
        http_status: int,
    ) -> None:
        """Emit a Langfuse trace for the OTB API fetch."""
        try:
            from src.observability.tracing import get_langfuse_client
            client = get_langfuse_client()
            if client is None:
                return

            with client.start_as_current_observation(
                name="otb/fetch_markets",
                as_type="span",
                input={
                    "url": url,
                    "status_filter": status_filter,
                    "page": page,
                    "page_size": page_size,
                },
            ):
                try:
                    from langfuse import get_client as _get_client
                    _lc = _get_client()
                    if _lc:
                        _lc.update_current_span(output={
                            "items_fetched": items_fetched,
                            "total_available": total_available,
                            "latency_ms": round(latency_ms, 1),
                            "http_status": http_status,
                        })
                except Exception:
                    pass
        except Exception as e:
            logger.debug("OTB fetch trace skipped: %s", e)


# ── Parsing helpers ────────────────────────────────────────────────────


def _parse_item(raw: dict[str, Any]) -> OTBMarketItem:
    """Parse a raw API item dict into an OTBMarketItem.

    Args:
        raw: Raw item dict from the API response.

    Returns:
        Validated OTBMarketItem.
    """
    return OTBMarketItem(
        question_id=raw.get("question_id", ""),
        title=raw.get("title", ""),
        ancillary_text=raw.get("ancillary_text", ""),
        end_date=raw.get("end_date", ""),
        proposal_time=raw.get("proposal_time", ""),
        status=raw.get("status", ""),
        market_slug=raw.get("market_slug", ""),
        event_slug=raw.get("event_slug", ""),
        resolution_conditions=raw.get("resolution_conditions", ""),
        proposed_price=raw.get("proposed_price", "0"),
        settled_price=raw.get("settled_price", "0"),
        proposal_tx_hash=raw.get("proposal_tx_hash", ""),
        request_tx_hash=raw.get("request_tx_hash", ""),
        tags=tuple(raw.get("tags", [])),
        integrations=tuple(raw.get("integrations", [])),
        raw=raw,
    )
