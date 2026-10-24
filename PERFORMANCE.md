# Faster production screening without multiplying the account budget

## Scope and deployment status

Prepared against `main` commit `199835c42436b3c0a78c21f545a296f32931283e`.
This is a local, un-deployed change. It does not include or replace the separate
unmerged history-alignment work. No current run, raw dated Sheet, credential,
grant, production setting, schedule, or published output was changed while
developing/testing it. Deployment/publication requires approval.

## Production limits

The three existing production jobs retain the same `production-screener`
concurrency group, `queue: max`, and `cancel-in-progress: false`. They are
**not** run in parallel. Each scheduled trigger remains Saturday 03:00
`Europe/London`. Main and V2 still execute sequentially within their job.

Only production jobs set `FMP_REQUESTS_PER_MINUTE=1800` and
`FMP_CONCURRENCY=30`. The constructors actually read these settings. Without
them, clients retain a conservative 280/minute and 10 workers. Values above
1,800/minute or 30 workers are rejected, not silently accepted.

Every production HTTP attempt, including retries and the main screener's float
download, passes through one paced budget shared across clients in that Python
event loop. There are no token-bucket bursts or catch-up bursts after pauses.
Bulk gates are applied atomically with the ordinary request gate:

| Budget | Implementation | Confirmed Ultimate allowance |
| --- | --- | --- |
| All production requests | 1,800/minute, paced | 3,000/minute |
| Ordinary bulk | at least 10.1 seconds apart | one request/10 seconds |
| Profile / ETF bulk | at least 61 seconds apart | one request/60 seconds |

Profile/ETF bulk also consumes the ordinary bulk gate. Unknown CSV endpoints
are treated conservatively. A new production job waits 61 seconds after taking
the shared queue slot and before requesting data, draining the prior job's
ordinary minute and bulk windows. Main's existing 60-second inter-screen pause
is unchanged.

This leaves **1,200 calls/minute unallocated** for connected tools and other
account consumers. It is not a distributed account-wide limiter: unrelated
processes/tools cannot be controlled here. They must stay within that headroom
and observe their own bulk spacing. Do not start multiple high-rate manual
processes or remove the shared queue. If greater uncontrolled use is expected,
lower the production rate rather than assigning each workflow its own 1,800.

## Failure integrity

429 responses, including recognized quota errors returned as HTTP 200, apply a
shared cooldown of at least 60 seconds, respecting longer numeric or HTTP-date
`Retry-After`. Ordinary network/5xx failures retry with bounded backoff. Each
retry consumes the same budget. Authentication/access failures are not bypassed
through alternate routes. Diagnostics never contain keys, authenticated URLs,
or raw error bodies.

On exhausted quota retries, a circuit breaker stops further requests. Controlled
failure cleanup retains the serialized queue slot until any outstanding
provider cooldown expires, including when a sibling worker fails/cancels the
original waiter. A forced runner termination, external cancellation, or workflow
timeout can still interrupt this in-memory cooldown. After such an event,
inspect the provider's cooldown before authorizing a manual rerun; no persisted
cross-run state or new service was introduced.

Main/V2 no longer silently turn exhausted transport errors into missing stocks
or default valuation fields. They fail closed rather than publish a deceptively
complete partial result. Existing verified completion/resume/hashing code is
unchanged; existing partial-coverage rules on the two newer screens are also
unchanged.

## What becomes faster

The newer screens already used concurrent fetches, but were hard-capped at
280/minute. Their request-pacing ceiling increases by `1800 / 280 = 6.43x`.
For **10,000 ordinary HTTP attempts**, a pacing-only model changes about
35.7 minutes to 5.6 minutes. This is a simulation/calculation, not a production
wall-clock promise: latency, bulk waits, retries, financial calculation, Sheets
writes, and queue wait still contribute. Profile bulk's approximately three
minutes between the first and fourth parts are deliberately not sped up.

Main/V2 replace serial network waits and 55-second blocking sleeps every 299
requests with bounded concurrent fetches. Financial calculations are still
applied in original input order. At most 30 concurrent fetch units are buffered at
once (up to 60 HTTP payloads when V2 pairs metrics and cash flow; profile units
retain their existing symbol batches). A chunk waits for its slowest request before the next chunk, deliberately
trading a little peak speed for bounded memory and deterministic processing.

The universe, endpoint choices, record limits, financial thresholds, currencies,
freshness checks, reconciliation, ranking, cooldown rules, receipt logic, and
publication ordering are unchanged. No new data downloads or extra success-path
requests were introduced. Live results may still vary with data changing during
the different acquisition times; fixture parity is not a guarantee about future
market data.

## Bandwidth is separate

Ultimate's 150 GB trailing-30-day allowance is not raised by faster pacing.
Successful fixture runs have identical HTTP request multisets. No account-wide
bandwidth balance or other consumers' volume was available, and this patch
cannot guarantee the monthly allowance. Before deploying, review the account's
remaining bandwidth. Do not truncate the universe or silently omit records to
meet a bandwidth budget. Improved retries can consume additional traffic on
previously failed requests; repeated manual reruns also consume traffic.

## Offline verification

Tests use fake credentials, fake HTTP, and synthetic data only. New coverage:

- 3,601 requests occupy 120 simulated seconds at 1,800/minute; rolling-window
  pacing, no catch-up bursts, shared clients, and safe configuration bounds.
- Separate bulk-family spacing, atomic bulk/global gating through cooldown,
  cancellation without quota reservation leaks, and extended terminal drain.
- Both actual provider constructors consume the production environment values.
- Main/V2 every endpoint and retry budgeted, 429/5xx/timeouts/auth failures,
  bounded workers, cancellation/join, and sanitized errors.
- Exact main/V2 result, ordering, return value, and success-path HTTP request
  multiset match synthetic golden fixtures produced by pristine main, with
  1, 3, and 30 workers. This is regression coverage, not a live end-to-end run.
- Workflow guards retain queue, schedule, PR production exclusion and handoff
  spacing; existing completion, freshness, calculations and output tests run.

The historical `tests/test_v2.py` is a pytest/live-account integration suite,
not an offline production-V3 test. It is intentionally not run against real
credentials or Sheets. The local environment also lacks pytest. Use the
explicit offline runner `python scripts/test_offline.py`; it fails network
connect attempts and reports this exclusion rather than calling it a pass.

## Approval and rollout

1. Review this local patch and confirm account bandwidth/headroom.
2. Obtain explicit approval to publish a draft PR; review its offline CI.
3. Obtain deployment/merge approval separately. Do not interrupt, restart, or
   dispatch today's in-flight jobs. Keep the shared queue and 03:00 UK schedule.
4. Observe the next authorized scheduled run's completion receipts and quota
   errors. Revert the patch or lower rate/concurrency if needed; do not loosen
   criteria or omit unresolved coverage to hit a duration target.

Official limits rechecked 2026-10-24:
[FMP pricing](https://site.financialmodelingprep.com/pricing-plans) and
[FMP FAQs](https://site.financialmodelingprep.com/faqs).
