# Next Steps — Production Scaling

> Discussion document. What changes when this moves from prototype to production oracle system.

---

## 1. Operational Observability

Per-case traces work for debugging. Production needs aggregated views:

- **Source freshness dashboard.** Track Wunderground API latency, error rates, and response sizes per station over time. A station that goes from 200ms → 2s or 200 → 503 needs to surface before it causes `unclear` cascades.
- **Confidence drift.** Plot per-market-type confidence distributions week-over-week. If Tokyo temperature markets drop from 0.95 to 0.70, either the source data quality is degrading or the normalization rules need recalibration.
- **Unclear rate alerting.** Threshold: if >X% of cases return `unclear` in a rolling window, something is wrong (source degradation, model failure, schema change). Alert operator; auto-pause live resolution if above critical threshold.
- **Settlement reconciliation.** For settled markets (Polymarket/UMA DVM), compare our paper-propose against the actual outcome. Track false-positive rate (wrong confident p1/p2) as the primary health metric. False negatives (unclear on a resolvable case) are secondary.
- **Cost tracking.** Per-market LLM token usage × model pricing. Reviewer invocation rate (should be low — if it's firing on >20% of cases, confidence scoring needs tuning).

---

## 2. Human Circuit Breaker

The LLM reviewer is a safety valve, but it can only escalate to `unclear`. Production needs a human in the loop for cases the system itself flags as untrusted:

- **Escalation triggers:**
  - Confidence < 0.70 after reviewer (the system is uncertain AND the reviewer couldn't help)
  - LLM reviewer disagrees with deterministic result (reviewer output: `{"agree": false}`)
  - New source type or measurement type never seen before (not in `MEASUREMENT_TYPES` enum)
  - Bulletin board update detected mid-resolution
  - Retrieval exhausted all fallback paths (API → Playwright → both failed)
  - Physical anomaly flag raised (value outside known limits — possible sensor error, not weather)

- **Operator interface (minimal):** Queue of flagged cases. Each shows the full evidence chain (what was fetched, normalized value, quality flags, deterministic recommendation, reviewer output). Operator actions: **Approve** (accept recommendation), **Override** (set different recommendation + reasoning), **Unclear** (confirm unclear), **Re-retrieve** (try fetching again — maybe transient failure). Every operator action is audited (who, when, what, why).

- **Circuit breaker thresholds:** If >N cases escalate in M minutes, stop auto-resolution entirely. This prevents a systemic failure (e.g., Wunderground serving corrupted data) from producing a cascade of wrong answers before anyone notices.

---

## 3. Scaling

### Model fallback
Today: one model (`LITELLM_MODEL`) with retries. Production:
```
primary: deepseek-chat
  → fallback: gemini-2.5-flash (different provider)
    → fallback: claude-sonnet-4 (different provider)
      → exhausted: regex-only (deterministic, no LLM)
```
LiteLLM already supports fallback routing in `litellm_config.yaml`. The pipeline just needs awareness: record which model was used in the trace, reduce confidence slightly for fallback models.

### Service fallback
Today: Wunderground API → Playwright → `unclear`. Production adds:
- **Backup sources per station.** If Wunderground is down, try NOAA/NWS for the same station if available. Record source provenance in trace.
- **Stale data tolerance.** If Wunderground is down for a station but we have a fixture from <24h ago, use it with a `stale_data` quality flag and reduced confidence. Better than `unclear` on a deadline-approaching market.
- **Degraded mode.** If >50% of live retrievals are failing, auto-switch to replay-only mode and alert. Don't keep hammering a broken API.

### Priority queueing
Not all markets are equal. A market settling in 2 hours needs resolution before a backtest case:
- **Priority levels:** `critical` (settlement window closing < 1h), `high` (< 6h), `normal` (< 24h), `background` (backtesting/historical).
- **Starvation prevention:** background cases get processed when the queue is idle. Minimum processing guarantee per priority level.
- **OTB API polling** feeds into the queue with priority derived from proposal time and settlement deadline.

### Persistent state & crash recovery
Today: if the container dies mid-resolution, state is lost. Production needs:
- **Checkpoint after each stage.** Write `PipelineContext` state to disk/key-value store. On restart, check for incomplete cases and resume from last checkpoint.
- **Idempotent resolution.** If a case was resolved but the output wasn't written before crash, re-running should produce the same result (stored `RetrievalSpec` in checkpoint makes this deterministic).
- **Heartbeat + watchdog.** If no progress for >T seconds, assume stall and restart. External process monitors the resolver health.
- **At-least-once delivery.** OTB API polling uses cursor-based pagination or tracks last-seen proposal timestamp. On restart, re-fetch from last checkpoint to avoid missing markets.

---

## 4. Benchmark Expansion

The 5 visible cases are a start. Production needs a growing, diverse benchmark:

- **Capture settled markets.** Every time a Polymarket weather market settles, capture its resolution + our prediction. Add to benchmark. Automate this via Polymarket API/Gamma API.
- **Measurement diversity.** Current benchmark is all `temperature` + `max`/`min`. Add: precipitation (monthly totals, NOAA sourced), wind speed/gust, humidity, snow. Each measurement type has different units, precisions, and source types — the benchmark should cover all of them.
- **Edge case coverage.** Deliberately include:
  - `p4` cases (future date, finality not met)
  - `unclear` cases (corrupted fixtures, missing observations, unit conflicts)
  - Cross-station ambiguity (ancillary data mentions two stations)
  - Bulletin board updates (ancillary data changed mid-market)
  - Timezone edge cases (DST transitions, markets crossing midnight in station-local time)
- **Synthetic adversarial cases.** Generate fixtures with known answers and deliberately broken data to test that the system gates conservatively. E.g., a fixture with `temp: 999` should always go `unclear`, never `p1`/`p2`.
- **Continuous evaluation.** Run the resolver against the full benchmark weekly. Track: accuracy, unclear rate, false-positive rate. Flag regressions: any case that was correct last week and wrong this week triggers investigation.
- **Blind holdout set.** A portion of the benchmark is never used for development — only for final evaluation. Prevents overfitting resolution rules to known cases.

---

## 5. Detecting & Handling Repeated Failures

A single `unclear` is conservatism. A cascade of `unclear` is a system problem:

- **Failure definition.** A case "fails" if it returns `unclear` due to retrieval exhaustion or normalization error (not due to genuinely unresolvable data). A stage "fails" if it throws an exception that the `on_error` policy catches.
- **Per-stage circuit breaker.** If stage X (e.g., `retrieve`) fails for >N cases in a rolling window, stop processing and alert. This catches systemic issues (Wunderground down, LiteLLM proxy down) before they produce a wave of `unclear` results.
- **Per-station monitoring.** If a specific station (e.g., KBKF) is consistently returning partial data or errors while others are fine, flag the station as degraded. Route its cases to `unclear` immediately rather than burning retries.
- **Canary queries.** Periodically (every 5 min) query a known-good station+date (e.g., RJTT for a settled date with a known fixture). If the canary fails, the source is degraded — alert and pause live resolution. If the canary succeeds but with different data than the fixture, the source has changed — flag for investigation.
- **Dead letter queue.** Cases that fail resolution >3 times (after retries, fallbacks, and operator re-retrieval) move to a dead letter queue. Operator investigates: is the ancillary data malformed? Is the station URL dead? Has the source format changed? Fix the root cause, then replay from the dead letter queue.
- **Root cause categorization.** Every failure is tagged with a category: `source_unavailable`, `source_format_change`, `model_failure`, `data_quality`, `schema_violation`, `unknown`. Dashboards show failure distribution by category. A spike in `source_format_change` means Wunderground changed their API — need to update the scraper.
- **Automated recovery.** If the failure category is `model_failure` and a fallback model succeeds, auto-promote the fallback for the next N minutes. If `source_unavailable` and a backup source exists, auto-switch. Operator is notified but doesn't need to act immediately.
- **Postmortem → regression test.** Every production incident produces: (a) a new benchmark case that reproduces the failure, (b) a guardrail update if the failure could have been caught earlier, (c) a monitoring rule if the failure could have been detected automatically. This is how you "prevent repeat mistakes."
