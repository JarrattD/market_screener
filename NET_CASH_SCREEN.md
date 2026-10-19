# Net Cash & High Margin Screener

A separate global acquisition-candidate screen. The two original screeners, their
criteria, outputs and exclusions are unchanged.

## Exact criteria

All three base conditions must hold:

- Positive market capitalisation, converted at dated latest available FX, at or
  below GBP 75 million. No additional minimum size.
- Cash and short-term investments minus total debt is strictly positive. The
  combined cash field is deducted once; leases already in total debt are not
  added again.
- Latest completed annual gross profit / revenue is at least 30%.

At least **one** valuation condition must also hold:

- Market capitalisation / positive tangible book value <= 1; **OR**
- Market capitalisation / positive five-year average reported FCF <= 5; **OR**
- Screening enterprise value / positive five-year average reported FCF <= 5.

Tangible book value uses attributable stockholders' equity less goodwill and
other intangibles. Enterprise value uses market cap in financial reporting
currency plus total debt less cash and short-term investments. FCF is FMP's
reported `freeCashFlow`, not adjusted owner earnings. Use five completed,
consecutive annual financial periods including negative years. Provider FY classification
and adjacent year ends are used; explicit short/extended periods fail validation.
When the oldest start date and its preceding year end are unavailable, its exact
length is inferred from the FY classification and is disclosed as an assumption. A missing route
does not exclude a stock when another verified route passes. Non-positive FCF
means its multiples are not meaningful, not a cheapness pass. Negative EV is
retained and clearly qualified. Show all three route flags and the number
passed; more passes rank first, never create another filter.

## Coverage and data integrity

The provider's fresh global directory is used, not the original screeners'
country-filtered static universe. No profit, growth, FCF, country or top-N hurdle
is added beyond the chosen criteria. Funds, ETFs and non-equity instruments are
not operating-stock candidates. Unknown security eligibility is unresolved.
Previously seen qualifiers are not removed. Verified same-ISIN listing aliases
are collapsed; other possible issuer/share-class/ADR aliases require review
before generating duplicate reports.

Missing values are never assumed zero. Every provider-universe symbol is
accounted for as match, rejected, excluded security, duplicate listing or
unresolved, with API diagnostics retained privately. A coverage summary makes
unresolved inputs explicit. This is coverage of FMP's universe, not a claim that
every company on every exchange has data. GBp/GBX price units are not blindly
applied to capitalization; ambiguous units are unresolved. Dated FX, statement
periods, inputs, calculations and qualifications accompany qualifying rows.

## Scheduling and operations

`.github/workflows/net-cash-screen.yml` runs Saturdays at 03:00 Europe/London,
sharing the `production-screener` concurrency lock with the originals so that
their common FMP budget is not consumed simultaneously. GitHub may queue either
workflow first. `workflow_dispatch` supports an explicit full-universe test.
The one-off initial test push trigger is removed before deployment.
Pull requests run mocked/unit tests only and cannot write to Sheets.

The job reuses existing `FMP_KEY` and `SERVICE_ACCOUNT_JSON` secrets without
creating or changing credentials. Results go only to the distinct spreadsheet
named `Net Cash & High Margin Screener`, already shared with the existing
service account. No candidate rows or private sheet IDs are uploaded to public
GitHub artifacts or logs. Duplicate same-name destinations block rather than
silently choosing a file.

Each completed batch writes a date-named results tab, `Coverage DATE` and `Inputs
DATE`, plus a current detailed assessment audit. All writes are read back and
verified. Zero matches is a valid completed result when supported by coverage.
A conflicting pre-existing dated output is not overwritten automatically.
Missing input cases remain unresolved for investigation. An API-wide failure
must not be represented as a completed zero-result screen.

## Speed without changing criteria

Financial requests are ordered by the mandatory gates: annual margin first,
then both balance frequencies, then FCF history only for net-cash survivors.
Known failures stop early; missing data remain unresolved. Every survivor still
gets all three cheapness route flags. This does not replace the global universe
with a provider prefilter or reuse potentially stale financial data.

The offline three-stock benchmark (one margin failure, one net-cash failure,
one survivor) uses 8 financial requests instead of 12 with the same result.
This is a synthetic request-count example, not a measured live speedup. Actual
HTTP, retry and bulk request counts and data-stage elapsed time are recorded
privately in completed coverage output for comparison. No parallel live tests
should compete for the same API budget.

## Tests

`python -m unittest discover -s tests -p 'test_net_cash*.py' -v`

For an authorised live run only:
`python net_cash_screen.py --service-account PATH_TO_EXISTING_SERVICE_ACCOUNT`

These are screening leads, not investment recommendations or permission to
trade. Report review should check restricted/customer cash, lease completeness,
asset quality, durability, acquisition constraints and the main catch.

Correctness safeguards added after the baseline test began:
- Parse filing/revision timestamps chronologically. Conflicting same-period financial values without a reliable revision order remain unresolved; five-year history resolves all revisions together rather than letting API order select a value.
- A run-wide authentication/quota circuit breaker aborts publication even after some successful requests. Ordinary issuer-specific missing data still remains explicitly unresolved.
- Preflight completed results and dated companions before changing any output. New companion tabs are sealed with verified hashes and private per-run ownership. Existing baseline receipts remain readable only after all original result, coverage and input hashes verify.
- The initial live baseline predates these safeguards and requires separate quota, revision and completeness review before its results are accepted.

Queue policy is consistently `queue: max` and `cancel-in-progress: false`. The existing workflow received the separately approved one-line scheduling correction in PR48.

## Current-data validation after the baseline
The baseline finished in 5h29m with 1,342 raw matches. Its output is retained under Provisional tabs, not accepted as current candidates: all result rows lacked quoted price dates and some latest provider statements were obsolete. No provisional row should feed research.

Every active eligible listing now receives a fresh timestamped quote before the cap gate, including stocks whose former profile cap was above the limit. Missing quote data clears the old profile cap instead of falling back to it. Current quote age must be at most seven days; annual and latest balance periods must be within eighteen calendar months. These are documented data-quality grace windows, not investment filters: older/missing evidence is unresolved pending latest-filing verification. isActivelyTrading must explicitly be true; missing is unresolved and false excluded. No country or OTC blanket exclusion has been added.

Shared quote refresh and freshness modules are reviewed together across the new and existing screens. The original financial thresholds and inclusive valuation OR remain unchanged. New quotes retain their actual provider timestamp, source and raw evidence. Retrieval time is not a market-price date.

The temporary test-branch push event has been removed. Production remains Saturday 03:00 Europe/London with workflow_dispatch and queue:max / cancel-in-progress:false. A corrected full live run and final output verification remain required before claiming end-to-end completion.
