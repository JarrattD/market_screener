"""End-to-end screening fixtures with all providers and Sheets publication mocked."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import revenue_net_cash_screen as screen
from screenerV3.negative_ev_provider import FXRate

NOW = datetime.now(timezone.utc)
CURRENT = NOW.date().isoformat()
BATCH = date(2026, 10, 26)


def profile(symbol):
    return dict(symbol=symbol, companyName='Fixture '+symbol, country='ZZ',
                exchangeShortName='TEST', cap_amount='10', cap_currency='USD',
                cap_currency_basis='trading_currency', security_eligible=True,
                security_identity='SYMBOL:'+symbol, as_of=NOW.isoformat())


def income(symbol):
    return SimpleNamespace(latest=dict(symbol=symbol, date=CURRENT, period='FY',
        fiscalYear=str(NOW.year), reportedCurrency='USD', revenue='100', grossProfit='30'),
        error=None, fetched_at=NOW.isoformat())


def balance(symbol):
    return dict(symbol=symbol, date=CURRENT, period='Q2', reportedCurrency='EUR',
                totalDebt='10', cashAndShortTermInvestments='11')


class FixtureClient:
    universe_source = 'offline full fixture universe'

    def __init__(self, symbols=('ONE',)):
        self.universe = [{'symbol': s} for s in symbols]
        self.profiles = {s: profile(s) for s in symbols}
        self.incomes = {s: income(s) for s in symbols}
        self.balances = {s: balance(s) for s in symbols}
        self.health, self.metrics, self.diagnostics = {}, {}, []
        self.calls = []
        self.issuer_failures = set()
        self.issues = []
        self.fx_rates = {}

    async def fetch_universe(self):
        self.calls.append(('universe', None))
        return deepcopy(self.universe)

    async def fetch_profiles(self, universe):
        self.calls.append(('profiles', [r['symbol'] for r in universe]))
        return deepcopy(self.profiles), list(self.issues)

    async def fetch_annual_income(self, symbols):
        self.calls.append(('income', list(symbols)))
        return {s: deepcopy(self.incomes[s]) for s in symbols if s in self.incomes}, []

    async def verify_issuer_caps(self, profiles):
        self.calls.append(('issuer', list(profiles)))
        result = deepcopy(profiles)
        for s, p in result.items():
            p['whole_issuer_cap_verified'] = s not in self.issuer_failures
            p['whole_issuer_cap_evidence'] = dict(scope='whole_issuer', validated=True,
                cap_amount=p['cap_amount'], cap_currency=p['cap_currency'], source='offline issuer proof')
        return result, []

    async def fetch_fx(self, currencies):
        self.calls.append(('fx', set(currencies)))
        return deepcopy(self.fx_rates), []

    async def fetch_balances(self, symbols):
        self.calls.append(('balance', list(symbols)))
        return {s: deepcopy(self.balances[s]) for s in symbols if s in self.balances}, []


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    async def assess(self, client):
        with patch.object(screen, 'publish', return_value={'verified': True}) as publish:
            receipt = await screen.screen(client, object(), BATCH)
            self.assertEqual(receipt, {'verified': True})
            publish.assert_called_once()
            return publish.call_args.args

    async def test_inclusive_three_filters_and_independent_balance_currency(self):
        args = await self.assess(FixtureClient())
        rows, audit, coverage, config, inputs = args[2:]
        self.assertEqual(args[1], BATCH)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), len(rows[1]))
        self.assertEqual(rows[1][4:7], [Decimal('.1'), Decimal('.3'), Decimal('1')])
        self.assertEqual(rows[1][10], 'USD')
        self.assertEqual(rows[1][15], 'EUR')
        self.assertEqual(coverage['status'], 'complete')
        self.assertEqual(coverage['matched_rows'], 1)
        self.assertEqual(config['additional_financial_filters'], [])
        self.assertEqual({r[1] for r in inputs[1:]}, {'Profile', 'Latest annual income', 'Latest balance sheet', 'Screen calculations'})

    async def test_each_candidate_stage_and_all_symbols_accounted(self):
        symbols = ('GOOD', 'NO_PROFILE', 'EXCLUDED', 'UNKNOWN_SECURITY', 'NO_INCOME',
                   'BAD_INCOME', 'LOW_MARGIN', 'NO_SCOPE', 'HIGH_CAP', 'NO_BALANCE',
                   'BAD_BALANCE', 'ZERO_NET_CASH')
        c = FixtureClient(symbols)
        del c.profiles['NO_PROFILE']
        c.profiles['EXCLUDED'].update(security_eligible=False, security_reason='inactive')
        c.profiles['UNKNOWN_SECURITY']['security_eligible'] = None
        del c.incomes['NO_INCOME']
        c.incomes['BAD_INCOME'].latest['grossProfit'] = None
        c.incomes['LOW_MARGIN'].latest['grossProfit'] = '29.999'
        c.issuer_failures.add('NO_SCOPE')
        c.profiles['HIGH_CAP']['cap_amount'] = '10.0001'
        del c.balances['NO_BALANCE']
        c.balances['BAD_BALANCE']['totalDebt'] = None
        c.balances['ZERO_NET_CASH']['cashAndShortTermInvestments'] = '10'
        args = await self.assess(c)
        status = {r[0]: r[1] for r in args[3][1:]}
        self.assertEqual(set(status), set(symbols))
        self.assertEqual(status['GOOD'], 'match')
        self.assertEqual(status['EXCLUDED'], 'excluded_security')
        for s in ('LOW_MARGIN', 'HIGH_CAP', 'ZERO_NET_CASH'):
            self.assertEqual(status[s], 'rejected')
        for s in ('NO_PROFILE', 'UNKNOWN_SECURITY', 'NO_INCOME', 'BAD_INCOME',
                  'NO_SCOPE', 'NO_BALANCE', 'BAD_BALANCE'):
            self.assertEqual(status[s], 'unresolved')
        stages = dict(c.calls)
        self.assertNotIn('LOW_MARGIN', stages['issuer'])
        self.assertNotIn('HIGH_CAP', stages['balance'])
        self.assertNotIn('NO_SCOPE', stages['balance'])
        self.assertEqual([r[0] for r in args[2][1:]], ['GOOD'])
        self.assertEqual(args[4]['status'], 'complete_with_unresolved')

    async def test_no_cap_ceiling_and_no_positive_fcf_equity_requirement(self):
        c = FixtureClient()
        c.profiles['ONE']['cap_amount'] = '100000000000'
        c.incomes['ONE'].latest.update(revenue='1000000000000', grossProfit='300000000000',
                                      operatingIncome=-1, ebit=-10)
        c.balances['ONE'].update(totalStockholdersEquity=-100, freeCashFlow=-100)
        args = await self.assess(c)
        self.assertEqual(args[4]['matched_rows'], 1)
        self.assertFalse(any('cashflow' in name for name, _ in c.calls))

    async def test_latest_income_failure_does_not_fall_back(self):
        for latest, error in ((None, None), (income('ONE').latest, 'annual request failed')):
            c = FixtureClient()
            c.incomes['ONE'].latest, c.incomes['ONE'].error = latest, error
            args = await self.assess(c)
            self.assertEqual(args[4]['unresolved'], 1)
            self.assertEqual(dict(c.calls)['balance'], [])

    async def test_stale_or_future_statement_unresolved(self):
        for stage in ('income', 'balance'):
            for value in ('2010-01-01', '2999-01-01', ''):
                c = FixtureClient()
                target = c.incomes['ONE'].latest if stage == 'income' else c.balances['ONE']
                target['date'] = value
                args = await self.assess(c)
                self.assertEqual(args[4]['unresolved'], 1)
                self.assertEqual(args[4]['matched_rows'], 0)

    async def test_missing_fx_unresolved_not_zero(self):
        c = FixtureClient()
        c.profiles['ONE']['cap_currency'] = 'EUR'
        args = await self.assess(c)
        self.assertEqual(args[4]['unresolved'], 1)
        self.assertEqual(dict(c.calls)['balance'], [])

    async def test_foreign_cap_threshold_and_fx_inputs_recorded(self):
        c = FixtureClient()
        c.profiles['ONE'].update(cap_amount='8', cap_currency='EUR')
        c.fx_rates = {currency: FXRate(currency, rate, CURRENT, NOW.isoformat(), 'fixture')
                      for currency, rate in (('EUR', 1), ('USD', .8))}
        args = await self.assess(c)
        self.assertEqual(args[4]['matched_rows'], 1)
        self.assertEqual(args[2][1][22], Decimal('1.25'))
        self.assertEqual(len([row for row in args[6][1:] if row[1] == 'FX']), 2)

    async def test_verified_duplicate_security_collapsed_not_alias_guess(self):
        c = FixtureClient(('B', 'A', 'C'))
        for p in c.profiles.values():
            p.update(companyName='Same issuer', cik='00001')
        c.profiles['A']['security_identity'] = 'ISIN:one'
        c.profiles['B']['security_identity'] = 'ISIN:one'
        args = await self.assess(c)
        self.assertEqual([r[0] for r in args[2][1:]], ['A', 'C'])
        self.assertEqual(args[4]['duplicate_listing'], 1)
        self.assertEqual(args[2][1][26], 'C')
        self.assertEqual(args[2][2][26], 'A')

    async def test_valid_zero_results_is_complete(self):
        c = FixtureClient()
        c.incomes['ONE'].latest['grossProfit'] = '29'
        args = await self.assess(c)
        self.assertEqual(args[2], [screen.HEADERS])
        self.assertEqual(args[4]['status'], 'complete')
        self.assertEqual(args[4]['matched_rows'], 0)
        self.assertEqual(args[6], [['Ticker', 'Record', 'Provider data / calculation JSON', 'Retrieved UTC']])

    async def test_systemic_failure_empty_universe_or_no_profiles_never_publish(self):
        for mode in ('systemic', 'universe', 'profiles'):
            c = FixtureClient()
            if mode == 'systemic': c.health['systemic_failure'] = True
            elif mode == 'universe': c.universe = []
            else: c.profiles = {}
            with patch.object(screen, 'publish') as publish:
                with self.assertRaises(ValueError):
                    await screen.screen(c, object(), BATCH)
                publish.assert_not_called()

    async def test_quota_fatal_after_acquisition_never_publish(self):
        c = FixtureClient()
        original = c.fetch_balances
        async def failed(symbols):
            value = await original(symbols)
            c._fatal = RuntimeError('quota exhausted')
            return value
        c.fetch_balances = failed
        with patch.object(screen, 'publish') as publish:
            with self.assertRaisesRegex(RuntimeError, 'quota exhausted'):
                await screen.screen(c, object(), BATCH)
            publish.assert_not_called()

    async def test_issue_preserved_in_audit_and_status(self):
        c = FixtureClient()
        c.issues = [SimpleNamespace(symbol='ONE', stage='profile', reason='diagnostic')]
        args = await self.assess(c)
        self.assertEqual(args[4]['status'], 'complete_with_unresolved')
        self.assertEqual(args[4]['data_issue_count'], 1)
        self.assertIn(['ONE', 'data_issue', 'profile: diagnostic', 'FMP'], args[3])

    async def test_verified_resume_does_not_acquire_data(self):
        c = FixtureClient()
        with patch.object(screen, 'open_destination', return_value=object()), \
             patch.object(screen, 'london_batch_date', return_value=BATCH), \
             patch.object(screen, 'resume_complete', return_value={'receipt': 'verified'}):
            self.assertEqual(await screen.run('mock-credentials', 'mock-id', c), {'receipt': 'verified'})
        self.assertEqual(c.calls, [])


if __name__ == '__main__':
    unittest.main()
