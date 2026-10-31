# Low Revenue Multiple & Net Cash Screener

This independent global screen adds no ownership data and changes no existing screen's financial criteria.

## Four entry gates

1. Verified positive whole-issuer market capitalization, converted into the latest completed annual income statement's currency, is **less than or equal to 10%** of positive annual revenue.
2. Gross profit divided by revenue for that **same FY** is **at least 30%**.
3. Whole-issuer market capitalization is **strictly below US$100,000,000**, using verified dated FX for non-USD caps; exactly US$100m does not qualify. No local-currency ceiling is substituted.
4. Cash and short-term investments on the latest available completed annual/interim balance sheet **strictly exceed total debt**.

Exactly 10% and exactly 30% qualify. Net cash of exactly zero does not. No FCF, equity, valuation-ranking, country or sector gate is added. Missing USD-conversion FX remains unresolved even when cap and revenue share a non-USD currency. Invalid/missing evidence is unresolved, not zero. Fresh provider global directory is used without top-N truncation; coverage is not a guarantee of every issuer worldwide.

The combined cash field is used once, otherwise both explicit components are required. Total debt is used once, without adding leases again. Contradictory combined/component values remain unresolved. Newest periods and newest verified revisions are selected; conflicting or future evidence cannot be replaced by an older favorable observation. Both balance frequencies must succeed. Latest accounts must meet the existing 18-calendar-month freshness policy; quotes and FX meet existing seven-day policy.

Quote cap is refreshed before use and separately reconciled against current outstanding shares, not float shares. Price units and cap currency are distinct. Provider-internal reconciliation within 1% does not constitute an independent issuer audit. Unverified ADR ratios or indicated multiple-share-class scope are unresolved. There are no ownership/holder endpoints or filters.

Criteria update requested 30 October 2026 (effective when deployed to main): adds the strict USD cap; prior completed outputs retain their original criteria and are not rewritten.

## Publication and ranking

Only the configured private workbook with exact title `Low Revenue Multiple & Net Cash Screener` may be written. Each successful batch writes dated results, sealed `Coverage DATE` and `Inputs DATE`, and latest assessment audit. Result receipt prefix is `revenue-net-cash-v1:`; companion seal prefix is `revenue-net-cash-sealed-v1:`. All matrices are read back before completion, and completed dated batches are immutable. Criteria dictionaries use sorted-key JSON hashes; matrix hashes use exact string cells padded to their width. Receipts include workbook/tab identity, batch date, origin run/attempt, workflow ref, commit, row count and content/coverage/input hashes.

Only verified previously published appearances in this workbook suppress another appearance until their twelve-calendar-month anniversary (February 29 clamps to February 28). Helpers, inputs, coverage, audits and unverified manual tabs are not appearances. Same-batch resume does not suppress itself. Coverage records `qualifying_rows_before_cooldown`, `cooldown_suppressed_rows` and `matched_rows`. Assessments and sealed evidence retain all financial qualifiers. New raw results are placed immediately after `Revenue Net Cash Ranked`.

The separate existing successful-run enrichment routine handles `Revenue Net Cash Ranked`, `Revenue Net Cash Run Map`, `Revenue Net Cash Metrics` and `Revenue Net Cash Refresh Log`. It uses the established six ratios, colours, green-count order, run selector, preferred-book rules and honest missing/Inactive/Check states. Ranking inputs are not additional entry criteria. There is no new ranking schedule, research or email delivery. Until a verified run is available, the dashboard explicitly awaits its first completed run.

## Operations

Saturday 03:00 Europe/London joins the existing pipeline. The new workflow waits outside the production lock for the matching scheduled Main/V2 success, then enters shared `production-screener` with `queue: max`, cancellation disabled, 61-second budget handoff, 1,800 requests/minute, 30 workers and separate bulk limits. Existing screens keep their schedules. Manual main-branch dispatch is a deliberate standalone exception to scheduled ordering. PR events only test.

Reuse `FMP_KEY` and `SERVICE_ACCOUNT_JSON`; configure the private destination identifier in `REVENUE_NET_CASH_SHEET_ID`. No destination ID, candidates or credentials are published in source/artifacts. Service-account access must be separately approved. No full live scan is run by offline tests.

Run `python scripts/test_offline.py` for the entire network-forbidden suite; historical live-only `tests/test_v2.py` is explicitly excluded. A successful offline/CI run is not evidence of live-provider completeness. First live completion must be verified by source run identity, exact receipt and full private Sheets hashes before claiming results are operationally verified.

