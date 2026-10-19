# Low Book Value and High Gross Margin Screen

This is a separate fifth screen. It does not change the existing screens.

A listing must satisfy all three conditions:

1. Verified positive market capitalization, converted to USD, at or below $75m.
2. Positive ordinary attributable book equity and market cap / that equity at or below 0.5x.
3. The newest completed publicly filed annual period has positive revenue and gross profit / revenue at or above 30%.

There is no net-cash, FCF, operating-margin, five-year-history, sector, country or liquidity gate. Common equity and ADR listings are eligible. Explicit funds, inactive listings and non-common instruments are not companies in this screen. Unknown security metadata remains unresolved.

## Book and margin definitions

Explicit commonStockEquity is already ordinary equity. Otherwise use totalStockholdersEquity less explicit preferredStock. Parent stockholders' equity already excludes noncontrolling interests; never subtract them twice. If deriving parent equity from totalEquity, an explicit minorityInterest/noncontrollingInterest is required. Missing preferred evidence is not zero. Conflicting identities are unresolved. Reconciliation allows only one reporting-currency unit of rounding; threshold comparisons have no tolerance.

Goodwill and intangibles remain in book value. This is not tangible book, liquidation value, distributable cash or an acquisition valuation. Cash, investments, receivables, inventory, goodwill, intangibles, property/plant/equipment, other assets and liabilities are diagnostics, not extra filters.

Gross margin uses grossProfit / revenue from the same FY. operatingIncome / revenue is displayed separately from the provider's ebit. Neither operating income nor EBIT is substituted for gross profit. Missing optional fields stay blank. Up to five unique annual gross margins are displayed only where data is available.

## Periods and currencies

Choose the newest available completed annual income period, then the latest accepted/filed revision. The balance is the newest completed annual or interim period. Conflicting equal-date records are unresolved; future financial periods or filings are excluded. Statement dates, filing dates and age warnings are shown. A stale provider period is visibly qualified, not silently described as current or replaced by a zero.

Profiles preserve price currency versus cap currency. A GBp/GBX price does not imply a cap should be divided by 100. Current price, current shares outstanding and a recent quote must reconcile the cap to whole GBP or pence. Unresolved units remain unresolved. Currency cross-rates have explicit direction and timestamp. Missing, nonpositive, future or older-than-seven-days FX quotes are never replaced with 1. USD identity needs no external FX quote.

## Efficient and private operations

Use one paced client, at most 280 requests/minute, bounded worker concurrency, a separate one-bulk-download/minute limiter, Retry-After handling and a run-wide exhausted-quota circuit breaker. The screen reads profiles, gates on USD market cap, reads annual income only for survivors, and reads balance sheets only after the gross-margin gate. The annual response supplies the optional history without an extra request. It never needs cash-flow endpoints.

Every supplied universe symbol is accounted as match, rejected, unresolved, excluded security or verified duplicate listing. Same-ISIN listings are deduplicated; possible issuer aliases are shown, not guessed away. The universe is FMP's fresh worldwide directory, not a previous result list or a top-N sample. This cannot guarantee coverage of every issuer worldwide.

Production runs share the repository's production-screener concurrency group with queue:max and cancel-in-progress:false. Saturday 03:00 Europe/London scheduling follows daylight saving. Manual runs are allowed only from main. Existing FMP_KEY and SERVICE_ACCOUNT_JSON secrets are used without changing or printing credentials. No new credentials or public result artifacts are created.

Only the explicitly configured private results Sheet with its expected exact title may be written. Successful runs append a dated results tab, dated coverage and inputs, and a latest assessment audit. Completion requires full read-back hashes. Completed dated output and sealed companions are immutable; a retry resumes only a matching verified batch. A failed or systemic-data run cannot publish a completed zero-result batch. A complete_with_unresolved batch explicitly does not claim full data coverage.

## Tests and operation

Run offline tests from the repository root:

    python -m unittest discover -s tests -p 'test_book_margin*.py' -v
    python -m unittest discover -s tests -p 'test_completion.py' -v

Run an authorized live refresh:

    python book_margin_screen.py --service-account PATH --spreadsheet-id VERIFIED_PRIVATE_SHEET_ID

Offline and CI tests are not proof of live provider completeness. A live run must finish and its dated Sheet receipt, row count and coverage must be read back before declaring it operationally verified.
