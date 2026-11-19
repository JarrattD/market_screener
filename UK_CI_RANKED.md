# UK & Channel Islands Ranked

In GitHub Actions choose **UK & Channel Islands Ranked → Run workflow → main**.
This workflow has no schedule, no automatic triggers and no dependence on the
other screens' latest results. It shares the production FMP request queue so it
cannot run alongside another production screen using the same account.

The workflow uses the existing `FMP_KEY` and `SERVICE_ACCOUNT_JSON` secrets. The
existing service account must be a writer on exactly one private spreadsheet
titled **UK & Channel Islands Ranked**. The workbook template must have **Ranked**,
**HQ Review** and **Coverage**, with `Rank`, `Ticker` and `Metric` in the
corresponding A1 cells and `Template | uk-ci-ranked-v1` in Coverage A2:B2.
No spreadsheet locator, results or security-level logs are published to GitHub.

- **Listings:** official London Stock Exchange Main Market and AIM equity
  instrument lists, plus the official Aquis issuer list. One row per issuer
  across venues/share classes; includes listed closed-end funds and REITs,
  not debt/ETFs; no market-cap floor or ceiling. The LSE source is a month-end
  snapshot, with its date in Coverage; Aquis uses its current roster feed.
- **Group headquarters** can be anywhere in the United Kingdom, Jersey or
  Guernsey. Foreign operations, customers or incorporation do not disqualify
  it. FMP profile evidence is provisional; ambiguous locations and absent
  profiles remain in **HQ Review**, never inferred from an ISIN prefix.
  The small static override file accepts sourced exceptional decisions.
- **Fast scoring:** one pass with FMP bulk profile matching, batched quotes,
  bounded concurrent inputs and reuse of FX; six standard financial checks
  with a seventh point for supported ownership. Financial warnings belong
  in Notes, not in a hard filter. Unusable, incompatible or missing inputs
  affect only the checks that depend on them. FCF starts from operating cash
  flow less capex; it is not always the same as adjusted owner cash flow.
- **Ownership Evidence** is an optional private sheet with headers
  `ISIN, Ticker (market), Largest holder % (0–100), As of (YYYY-MM-DD), Source
  URL, Holder, Notes`. Use only dated, checked evidence of the largest
  disclosed beneficial holder, not an arbitrary holding or a nominee.
  Conflicting or expired entries earn no point. Missing ownership does not
  prevent scoring the financial checks.
- **Ranked** is a numbered list, highest score first, with ties alphabetical.
  There is a difference between a legitimate 0/7 and a blank score (no
  supported checks). A new run replaces the generated data; personal work
  should be kept in a separate tab or separate document.

Local validation: run `python -m unittest discover -s tests -p 'test_uk_ci*.py' -v`.
A protected environment with `FMP_KEY` can run `python uk_ci_ranked.py --dry-run`
for a live read-only check; it never opens or writes Google Sheets.
