import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from screenerV3.uk_ci_fmp import (
    _annual_periods, _pick_shares, _score_inputs,
    fetch_financials, fetch_listing_profiles, reconcile_cap,
)

NOW = datetime(2026, 11, 19, 18, tzinfo=timezone.utc)
ISIN = 'GB00EXAMPLE1'
SYMBOL = 'TEST.L'
PROFILE = {'symbol': SYMBOL, 'isin': ISIN, 'exchange': 'LSE', 'currency': 'GBp',
           'isFund': True, 'isActivelyTrading': True}
QUOTE = {'symbol': SYMBOL, 'timestamp': NOW.timestamp(), 'price': 250, 'marketCap': 250000}
SHARES = {'symbol': SYMBOL, 'date': '2026-11-19 04:36:30', 'outstandingShares': 100000,
          'floatShares': 90000, 'source': 'https://issuer.example/disclosures'}
BALANCE = {'symbol': SYMBOL, 'date': '2026-06-30', 'period': 'Q2', 'reportedCurrency': 'GBP',
           'totalCurrentAssets': 410000, 'totalLiabilities': 100000, 'minorityInterest': 0,
           'totalStockholdersEquity': 350000, 'preferredStock': 0,
           'goodwillAndIntangibleAssets': 0, 'totalDebt': 50000,
           'cashAndShortTermInvestments': 300000}


def fy(y, **kw):
    return {'symbol': SYMBOL, 'date': f'{y}-12-31', 'period': 'FY',
            'reportedCurrency': 'GBP', 'operatingCashFlow': 100000,
            'capitalExpenditure': -10000, **kw}


class FakeFMP:
    use_profile_bulk = True
    concurrency = 2
    _fatal = None

    def __init__(self, shape=None):
        self.shape = shape or {}
        self.calls = []

    async def _request(self, path, params=None, **kw):
        self.calls.append((path, params))
        if path in self.shape:
            return self.shape[path](params)
        if path == '/stable/profile-bulk':
            return [PROFILE] if params['part'] == 0 else []
        if path == '/stable/batch-quote':
            return [QUOTE]
        if path == '/stable/shares-float':
            return [SHARES]
        if path == '/stable/balance-sheet-statement':
            return [BALANCE]
        if path == '/stable/cash-flow-statement':
            return [fy(y) for y in range(2025, 2020, -1)]
        return []


class FMPAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_matched_fund_retains_profiles_and_five_years(self):
        listing = SimpleNamespace(isin=ISIN, fmp_symbol=SYMBOL, company='Test Trust', market='AIM')
        api = FakeFMP()
        profiles = await fetch_listing_profiles(api, [listing])
        self.assertEqual(profiles, {ISIN: PROFILE})
        row = (await fetch_financials(api, profiles, now=NOW))[ISIN]
        self.assertEqual(row['status'], 'Available')
        self.assertEqual(row['score_inputs']['market_cap'], 250000)
        self.assertEqual(row['score_inputs']['annual_fcf'], [90000] * 5)
        self.assertIsNone(row['score_inputs']['ownership_pct'])
        self.assertEqual(row['shares']['outstandingShares'], 100000)
        self.assertEqual(row['currency'], 'GBP')

    async def test_wrong_isin_on_guessed_symbol_never_attaches(self):
        listing = SimpleNamespace(isin=ISIN, fmp_symbol=SYMBOL, company='Test Trust', market='Aquis')
        alt = dict(PROFILE, symbol='TRUE.L')
        api = FakeFMP({'/stable/profile-bulk': lambda _: [],
                       '/stable/profile': lambda p: [alt] if p['symbol'] == 'TRUE.L' else [
                           dict(PROFILE, isin='GB00OTHER001')],
                       '/stable/search-isin': lambda p: [
                           {'symbol': 'TRUE.L', 'isin': ISIN}, {'symbol': 'WRONG.AS', 'isin': ISIN}]})
        self.assertEqual(await fetch_listing_profiles(api, [listing]), {ISIN: alt})
        self.assertNotIn(('/stable/profile', {'symbol': 'WRONG.AS'}), api.calls)

    async def test_valuation_provisional_when_fmp_has_no_independent_shares(self):
        api = FakeFMP({'/stable/shares-float': lambda _: []})
        row = (await fetch_financials(api, {ISIN: PROFILE}, now=NOW))[ISIN]
        self.assertEqual(row['score_inputs']['market_cap'], 250000)
        self.assertEqual(row['score_inputs']['annual_fcf'][0], 90000)
        self.assertIn('warning_independent_price_or_outstanding_shares_not_available_fmp_cap_assumed_whole_GBP', row['notes'])

    def test_pence_cap_disambiguated_and_pure_mismatch_keeps_market_cap(self):
        self.assertEqual(reconcile_cap(PROFILE, QUOTE, SHARES), (250000.0, 'GBP', None))
        self.assertEqual(reconcile_cap(PROFILE, dict(QUOTE, marketCap=25000000), SHARES),
                         (250000.0, 'GBP', None))
        self.assertEqual(reconcile_cap(PROFILE, dict(QUOTE, marketCap=260000), SHARES)[0], 260000)
        self.assertIsNone(reconcile_cap(PROFILE, dict(QUOTE, currency='USD'), SHARES)[0])
        self.assertIsNone(reconcile_cap(dict(PROFILE, uk_screen_multiple_isins=True), QUOTE, SHARES)[0])
        self.assertIsNone(_pick_shares([dict(SHARES, source='')], SYMBOL, NOW))

    async def test_stale_matched_quote_still_scores_with_warning(self):
        api = FakeFMP({'/stable/batch-quote': lambda _: [dict(QUOTE, timestamp='2025-12-01')]})
        row = (await fetch_financials(api, {ISIN: PROFILE}, now=NOW))[ISIN]
        self.assertEqual(row['score_inputs']['market_cap'], 250000)
        self.assertIn('warning_stale_quote_preliminary', row['notes'])

    async def test_matched_profile_cap_fallback(self):
        api = FakeFMP({'/stable/batch-quote': lambda _: []})
        row = (await fetch_financials(api, {ISIN: dict(PROFILE, marketCap=250000, price=250)}, now=NOW))[ISIN]
        self.assertEqual(row['score_inputs']['market_cap'], 250000)
        self.assertIn('warning_matched_fmp_profile_cap_used_no_usable_quote', row['notes'])

    async def test_same_currency_duplicate_statement_can_score_provisionally(self):
        api = FakeFMP({'/stable/balance-sheet-statement':
                       lambda _: [BALANCE, dict(BALANCE, totalStockholdersEquity=370000)]})
        row = (await fetch_financials(api, {ISIN: PROFILE}, now=NOW))[ISIN]
        self.assertIn(row['score_inputs']['total_equity'], (350000, 370000))
        self.assertIn('warning_balance_numeric_revisions_disagree_preliminary', row['notes'])

    def test_gaps_changed_currency_and_conflicting_annuals_do_not_average(self):
        rows = [fy(2025, operatingCashFlow=None, freeCashFlow=-10),
                fy(2024, reportedCurrency='USD'), fy(2022), fy(2021), fy(2021, capitalExpenditure=-20)]
        annual, flags = _annual_periods(rows, SYMBOL, NOW)
        self.assertIn('warning_cashflow_numeric_revisions_disagree_preliminary:2021-12-31', flags)
        inputs, basis = _score_inputs(1, 'GBP', BALANCE, annual, {'GBP': 1, 'USD': .75}, NOW, flags)
        self.assertEqual(inputs['annual_fcf'], [-10, None, None, None])
        self.assertEqual(basis[0], 'Reported FCF fallback')
        self.assertIn('non_comparable_fiscal_year_interval', flags)


if __name__ == '__main__':
    unittest.main()
