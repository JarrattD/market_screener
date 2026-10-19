# Existing main and V2 data-quality safeguards

Investment thresholds, country/industry rules, output columns and output spreadsheets remain unchanged. This change validates the data supplied to those existing calculations:

- isActivelyTrading must explicitly be true. False is excluded inactive; missing is unresolved.
- Every eligible profile receives a current timestamped quote before any valuation gate. A missing, future or over-seven-day-old quote is unresolved, never a zero cap. The seven-day allowance covers weekends and market holidays.
- Latest annual cashflow and latest balance-sheet period ends must be within eighteen calendar months, allowing international annual-reporting delays. Older or missing evidence is unresolved pending the latest filing, not an investment rejection.
- Market-cap price units are reconciled, including GBp prices versus GBP market caps. The old engines assume financials and cap share a currency; unresolved mismatches are withheld rather than silently compared.
- Private Data quality DATE tabs record inactive exclusions, unresolved reasons and verified source dates. Main dated result columns are unchanged. Only exact date-named result tabs count toward previously-seen tickers, never audit or provisional tabs.
- Existing verified dated results are not rewritten or resent. Empty output still fails closed for investigation; no successful zero-result manifest is invented.

This is not a rewrite or full audit of legacy investment formulas. In particular, their existing key-metrics-TTM and mixed TTM/annual FCF method is preserved and should not be represented as independently verified sustainable owner FCF. Routine research must still check latest company filings and relevant asset/cash restrictions.
