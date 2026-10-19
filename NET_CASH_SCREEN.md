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
An exact-name temporary test-branch push trigger is used only for the authorised
initial live test and is removed from main once launched.
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

## Tests

`python -m unittest discover -s tests -p 'test_net_cash*.py' -v`

For an authorised live run only:
`python net_cash_screen.py --service-account PATH_TO_EXISTING_SERVICE_ACCOUNT`

These are screening leads, not investment recommendations or permission to
trade. Report review should check restricted/customer cash, lease completeness,
asset quality, durability, acquisition constraints and the main catch.
