# Fail-closed recovery for oversized profile request URLs

## Evidence (24 October 2026)

Main run `40100393978`, attempt 1, used commit
`2ca230bf5f7fb43a254ceda4cedb2a2e9c2244d5`. Its production step failed during
initial profile acquisition. The exception retained its HTTP status in memory
but omitted it from its string, so the exact historical status is unavailable.
No verified completion artifact was published.

The legacy 1,000-symbol profile batching predates the performance patch. Several
repository-universe batches produce URLs above 8 KB. Two diagnostic GETs without
any API key returned 401 for a short profile URL and 414 (URI Too Long) for the
longest repository batch (~9.9 KB). This demonstrates the current size problem;
it does not prove the unrecorded status of the failed production request.

## Narrow recovery

- Successful original requests and payload order remain unchanged.
- Only an explicit HTTP 414 from a multi-symbol profile request permits recovery.
  Split at the middle symbol boundary, fetch left then right recursively, and
  concatenate their list payloads in that order. Preserve duplicates and every
  symbol. Do not sort, truncate, skip, or substitute data.
- Every parent/child attempt goes through the existing shared limiter and retry
  rules. Sequential halves do not multiply the outer worker concurrency.
- A singleton 414, invalid split payload, or failure of either half fails the
  whole batch. No partial output becomes eligible for publication.
- Authentication, access/entitlement, quota and all other HTTP failures are not
  treated as length errors. Existing 429 cooldowns remain in force.
- Error strings now include a validated numeric HTTP status, never the request
  URL, API key, provider response body, or credentials.

Only transport handling and tests changed. Financial criteria, output/receipt
logic, ranking eligibility, schedules, permissions, account limits and the
Main-first workflow dependency remain unchanged. Failed oversized requests now
consume additional, budgeted split requests; successful requests incur none.

Offline tests exercise both complete repository ticker universes against a
simulated 8KB URI limit, recursive/singleton failures, duplicate/order integrity,
bounded concurrency, every attempt's budget, unchanged access/quota failures,
and failure-before-publication. The existing golden financial-output tests and
full offline regression suite also run. No authenticated FMP calls or Sheet
writes occur during testing. Production retry requires the approved deployment
and a fresh run on the corrected commit, not a rerun of the old commit.
