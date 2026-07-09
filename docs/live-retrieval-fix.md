# Fixing Live Wunderground Retrieval

## The problem

Live mode (`resolve.py --live`) fails at Stage 3 (retrieval). Two paths, both broken:

| Path | What happens | Why |
|---|---|---|
| API (`wunderground_api.py`) | HTTP 400 "location invalid" | Endpoint format or API key may need updating |
| Playwright (`wunderground_playwright.py`) | 0 rows scraped | Cookie consent popup intercepts clicks |

Everything **downstream** (normalization, reconciliation, decision, output) is unaffected — they consume a `RawObservationBatch` and don't care how it was produced.

## What to fix (contained change)

**File:** `src/retrieval/wunderground_playwright.py`

The Playwright scraper navigates to the Wunderground history page and tries to click the unit toggle, but a Sourcepoint cookie consent iframe (`#sp_message_iframe_1225696`) blocks pointer events.

Fix: dismiss the consent dialog before interacting with the page.

```python
# Pseudocode — add near the top of the Playwright navigation logic
try:
    consent_frame = page.frame_locator("#sp_message_iframe_1225696")
    reject_btn = consent_frame.locator("button[title='Reject All']")  # or similar
    reject_btn.click(timeout=5000)
    page.wait_for_timeout(1000)
except Exception:
    pass  # consent may not appear in all regions
```

Nothing else in the project needs to change.

## API path may not be dead

The 400 error says `ILA-0001: The location supplied is invalid`. The code uses format `RJTT:jp`. This could be:
- The API key (`e1f10a1e78da46f5b10a1e78da96f525`) was revoked/expired
- The endpoint URL format changed
- The location identifier format changed

It's worth investigating — check `src/retrieval/wunderground_api.py` for the exact URL construction. A working API would be faster and more reliable than Playwright.

## Verification

After fixing either path, smoke test:

```bash
python resolve.py --input data/markets.json --fixtures data/fixtures --live --case-id tokyo_low_2026_06_01_20c
```

Then run all 5 cases and evaluate:

```bash
python resolve.py --input data/markets.json --fixtures data/fixtures --live
python evaluate.py --predictions output/results.json --gold gold_visible/answers.json
```
