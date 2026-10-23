# Negative EV & Positive Five-Year FCF Screener

This is a separate global screen and a separate private Google Sheet. The
original screeners and the separate net-cash/high-margin screen are unchanged.
There is no dependency on the unmerged net-cash-screen branch.

## Only two financial filters

1. Screening enterprise value is strictly negative.
2. Cumulative reported free cash flow over the selected five-year interval is
   strictly positive.

No market-cap ceiling, gross-margin filter, country restriction, valuation
multiple or previously-seen exclusion is added. Zero is not a passing boundary.
Loss-making periods are included in the cumulative sum. Missing values are not
assumed zero. Funds, ETFs and non-equity securities are not operating stocks;
unknown security eligibility is unresolved. The fresh worldwide provider
directory is used without a top-N truncation.

## Five years without double-counting

Use the latest available cash-flow endpoint, not an older more favorable
endpoint. Prefer a verified, non-overlapping rolling five-year interval:

- Five completed FY totals, minus the matching opening-year YTD interval,
  plus the latest current-year YTD interval; or
- Twenty verified contiguous discrete quarters covering that same horizon.

For July 2021 through June 2026, the reconstruction is FY2021–FY2025 minus
January–June 2021 plus January–June 2026. This is not five FY plus TTM, nor four
FY plus an overlapping TTM observation. A reported TTM value can derive latest
YTD using TTM minus previous FY plus previous comparable YTD.

Quarterly labels alone do not prove discrete quarter cash flows. Q2/Q3 may be
cumulative. The calculation validates explicit period boundaries/basis, annual
reconciliations, and available TTM cross-checks. Ambiguous or inconsistent
interims cannot silently become a rolling result. Annual/quarter restatements
are one period, not additional observations.

If the interim boundaries cannot be established, use the last five completed
consecutive full FYs and explicitly label the method and fallback reason. The
actual start/end and all contributing signed components appear in the output.
Absent oldest period starts remain visibly unverified; no false exact-60-month
claim is made. Non-calendar 52/53-week fiscal periods are shown by their actual
dates rather than assumed to equal 60 calendar months. A missing required FY,
stub, fiscal-year gap or inconsistent historical currency is unresolved.

FCF uses the provider's reported freeCashFlow, not normalized owner earnings or
an investment forecast. The historical periods must share a consistent currency
or verified restatement basis. FCF currency does not need to equal the latest
balance-sheet currency because this screen tests the sum's sign, not EV/FCF.

## EV and currencies

Screening EV = market cap in balance-sheet reporting currency + total debt
minus cash and short-term investments. Use the combined cash field once; if
missing, require both separate components. Do not add leases to total debt
again automatically. Missing or invalid cap/debt/cash/FX remains unresolved.

Capitalization units are reconciled separately from quoted price units such as
GBp/GBX. Cross-currency EV uses a labeled directional rate built from dated
provider FX quotes. Same-currency EV does not need an optional GBP quote.

This simplified screening EV is not cash available to an acquirer. Restricted
or customer cash, lease inclusion, preferred/minority claims and accounting
changes need company-report review before drawing investment conclusions.

## Freshness and active-company validation

Only explicitly active operating-equity listings are eligible; inactive listings
are excluded and unknown activity remains unresolved. This is not an OTC,
country or market-cap exclusion. Before any valuation gate, every eligible
profile is refreshed from actual timestamped quotes. A profile download time
is not a price date, and its old cap is never retained on quote failure.
Quotes must be no more than seven days old, allowing weekends and holidays.
Batch quotes are reconciled symbol by symbol, and all caps remain currency-
verified. Subunit quotes need coherent current price, cap and shares evidence.

The latest balance-sheet period and selected five-year cash-flow endpoint must
be within 18 calendar months of assessment. The grace period accommodates
annual and cross-market reporting, but stale or undated evidence is unresolved
with a latest-filing verification reason, not a zero or financial rejection.
Historical periods inside the five-year sum are intentionally historical and
are not each subjected to a current-period age test. A recent quote cannot
make obsolete accounts current. The policy is versioned in the run receipt.

## Acquisition order and coverage

The runner loads the global directory/profiles, then both annual and interim
balance frequencies for every eligible stock. It requests cash-flow history
only for verified negative-EV survivors. It never requests income statements.
Eight annual, 32 interim and eight TTM records provide room for boundaries and
restatements; these are per-stock history limits, not a universe cap.

Both balance requests must succeed to establish the newest available balance.
An empty successful response is different from a failed request. Optional
interim/TTM failure may allow an explicitly documented five-FY fallback. Every
universe symbol is accounted for as match, rejected, excluded security,
duplicate listing, or unresolved. Verified same-security aliases are collapsed;
other potential issuer aliases are flagged rather than silently discarded.

There is one shared paced API limiter with retries and no credential-bearing
URLs in logs. This straightforward full-universe approach can take hours:
approximately two balance requests per eligible stock, plus three cash-flow
requests per negative-EV survivor and profile/FX overhead. Initial throughput
has not been measured. Do not claim a complete global run from a truncated or
timed-out attempt. Provider-wide failure blocks a completed result.

## Private output and activation status

The CLI requires an explicit spreadsheet ID and checks the expected distinct
title before writing. It reuses an existing service-account credential file;
it does not create credentials or grant access.

Completed runs write a dated results tab, Coverage DATE, Inputs DATE and a
Latest assessment audit. Read-back checks and content/criteria hashes verify
completion. Conflicting dated results are not automatically overwritten.
No candidate rows, raw inputs, credentials or private destination IDs are
included in public Actions artifacts or repository files.

The draft Actions workflow runs offline tests on pull requests. Its production
job is explicitly blocked for PR events, depends on successful tests, and can
run only on an approved manual dispatch or the Saturday 03:00 Europe/London
schedule after deployment to main. There is no push or issue trigger. The job
reuses existing FMP_KEY and SERVICE_ACCOUNT_JSON credentials and reads the
private destination ID from NEGATIVE_EV_SHEET_ID. This destination setting is
an identifier, not a new credential, and must be configured privately before
execution. No new credential or access grant is created by the workflow.

Production activation still requires approval for the service-account Sheet
grant, private destination configuration, first full run, schedule and any
main-branch merge. Coordinate API use with existing screeners. This job uses
production-screener, cancel-in-progress: false and queue: max; every participant
must use compatible queueing because false alone does not preserve multiple
waiting runs. The hosted job has a 350-minute safety timeout. The uncapped
global scan has not yet been live-tested; a timeout is not completion and may
require an approved longer-running environment or an equivalent-data
acquisition optimization before operational sign-off.

## Verification

Offline new-screen tests:

    python -m unittest discover -s tests -p 'test_negative_ev*.py' -v

Original completion regressions:

    python -m unittest discover -s tests -p 'test_completion.py' -v

Only after the destination and live run are authorized:

    python negative_ev_fcf_screen.py --service-account PATH --spreadsheet-id ID

Provider references checked 2026-10-19:
- https://site.financialmodelingprep.com/developer/docs/stable/cashflow-statement
- https://site.financialmodelingprep.com/developer/docs/stable/cashflow-statements-ttm
- https://site.financialmodelingprep.com/developer/docs/stable/balance-sheet-statement
- https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency

## Publication history and dashboard order

Qualifying stocks are published only when their last verified appearance in this
workbook is at least 12 calendar months old. The exact anniversary is eligible;
29 February anniversaries clamp to 28 February in non-leap years. Other screens,
dashboard refreshes, audit records, and incomplete/manual tabs do not count as
appearances. The raw financial criteria and assessments are unchanged.

Coverage reports `qualifying_rows_before_cooldown`, `cooldown_suppressed_rows`,
and published `matched_rows` separately. The full qualifying assessments and
input records remain private in the existing audit and Inputs tabs. Prior results
are immutable; historical completed rows and companion hashes/seals are verified
before they can suppress a new publication. Same-day verified reruns are idempotent.

When this workbook's Ranked dashboard is first, a new dated result is inserted
immediately beside it. Companion dashboards and existing manual tabs are not
rewritten by the screener. The existing schedule and destination are unchanged.
