"""Synthetic, exact arithmetic tests; no network or credentials."""
from copy import deepcopy
from decimal import Decimal, localcontext
import unittest
from screenerV3.revenue_net_cash_calculations import assess_revenue, net_cash

D = Decimal


def profile(cap='10', currency='USD', **changes):
    result = dict(cap_amount=cap, cap_currency=currency,
                  cap_currency_basis='trading_currency',
                  whole_issuer_cap_verified=True,
                  whole_issuer_cap_evidence=dict(scope='whole_issuer', validated=True,
                      source='verified fixture shares and classes', cap_amount=cap,
                      cap_currency=currency))
    result.update(changes)
    return result


def income(**changes):
    result = dict(date='2025-12-31', fiscalYear='2025', period='FY',
                  reportedCurrency='USD', revenue='100', grossProfit='30')
    result.update(changes)
    return result


def balance(**changes):
    result = dict(date='2026-06-30', reportedCurrency='USD', totalDebt='10',
                  cashAndShortTermInvestments='11')
    result.update(changes)
    return result


def fx(value, currency):
    return dict(rate_to_gbp=value, currency=currency, as_of='2026-10-26T10:00:00Z')


class RevenueTests(unittest.TestCase):
    def test_both_inclusive_boundaries_pass(self):
        result = assess_revenue(profile(), income(), {})
        self.assertEqual((result['status'], result['passes']), ('ok', True))
        self.assertEqual(result['market_cap_to_revenue'], D('.10'))
        self.assertEqual(result['gross_margin'], D('.30'))

    def test_exact_cap_boundary_without_rounding_tolerance(self):
        self.assertTrue(assess_revenue(profile('9.99999999999999999999999'), income(), {})['passes'])
        self.assertFalse(assess_revenue(profile('10.000000000000000000000001'), income(), {})['passes'])

    def test_exact_gross_margin_boundary_without_rounding(self):
        self.assertFalse(assess_revenue(profile(), income(grossProfit='29.999999999999999999999999'), {})['passes'])
        self.assertTrue(assess_revenue(profile(), income(grossProfit='30.000000000000000000000001'), {})['passes'])

    def test_no_cap_ceiling_equity_or_fcf_gate(self):
        p = profile('1000000000000')
        p.update(freeCashFlow=-1, totalStockholdersEquity=-1)
        self.assertTrue(assess_revenue(p, income(revenue='10000000000000', grossProfit='3000000000000', operatingIncome=-1), {})['passes'])

    def test_no_missing_or_nonfinite_or_bool_cap(self):
        for cap in (None, '', 'NaN', 'Infinity', True, False, 'bad', -1, 0, '1e1001'):
            with self.subTest(cap=cap):
                result = assess_revenue(profile(cap), income(), {})
                self.assertEqual(result['status'], 'missing')
                self.assertIsNone(result['passes'])

    def test_missing_or_invalid_revenue_and_profit(self):
        for field in ('revenue', 'grossProfit'):
            for value in (None, '', True, 'NaN', 'Infinity', '1e1001'):
                with self.subTest(field=field, value=value):
                    self.assertEqual(assess_revenue(profile(), income(**{field: value}), {})['status'], 'missing')
        for revenue in (0, -1):
            result = assess_revenue(profile(), income(revenue=revenue), {})
            self.assertEqual((result['status'], result['passes']), ('ok', False))

    def test_negative_gross_profit_fails_not_missing(self):
        result = assess_revenue(profile(), income(grossProfit=-30), {})
        self.assertEqual((result['status'], result['passes']), ('ok', False))

    def test_gross_profit_not_operating_income_or_ebit(self):
        result = assess_revenue(profile(), income(grossProfit=None, operatingIncome=100, ebit=100), {})
        self.assertEqual(result['status'], 'missing')

    def test_must_be_explicit_full_year(self):
        for period in ('TTM', 'Q4', None, ''):
            self.assertEqual(assess_revenue(profile(), income(period=period), {})['status'], 'missing')

    def test_no_raw_cap_or_unverified_listing_scope(self):
        for changes in ({'whole_issuer_cap_verified': False}, {'whole_issuer_cap_verified': None},
                        {'whole_issuer_cap_evidence': {}}, {'cap_amount': None, 'marketCap': 10},
                        {'cap_currency_basis': 'unresolved'}):
            self.assertEqual(assess_revenue(profile(**changes), income(), {})['status'], 'missing')
        for field, value in [('scope', 'listed_class'), ('validated', False), ('source', ''),
                             ('cap_amount', '9'), ('cap_currency', 'EUR')]:
            p = profile()
            p['whole_issuer_cap_evidence'][field] = value
            self.assertEqual(assess_revenue(p, income(), {})['status'], 'missing')

    def test_no_currency_guess(self):
        for c in (None, '', 'GBp', 'GBX', 'US', 123):
            self.assertEqual(assess_revenue(profile(currency=c), income(), {})['status'], 'missing')
            self.assertEqual(assess_revenue(profile(), income(reportedCurrency=c), {})['status'], 'missing')

    def test_pence_price_not_double_converted(self):
        p = profile('10', 'GBP', price='100')
        p['currency'] = 'GBp'
        self.assertTrue(assess_revenue(p, income(reportedCurrency='GBP'), {})['passes'])

    def test_fx_direction_and_exact_cross_currency_threshold(self):
        rates = {'EUR': fx('1', 'EUR'), 'USD': fx('.8', 'USD')}
        result = assess_revenue(profile('8', 'EUR'), income(), rates)
        self.assertTrue(result['passes'])
        self.assertEqual(result['market_cap_reporting'], D(10))
        self.assertEqual(result['cap_to_income_fx_rate'], D('1.25'))
        self.assertFalse(assess_revenue(profile('8.000000000000000000000001', 'EUR'), income(), rates)['passes'])

    def test_same_currency_requires_no_optional_fx(self):
        self.assertTrue(assess_revenue(profile('10', 'JPY'), income(reportedCurrency='JPY'), {})['passes'])

    def test_fx_missing_invalid_undated_or_mismatched(self):
        for rates in ({}, {'EUR': fx(0, 'EUR')}, {'EUR': fx('NaN', 'EUR')},
                      {'EUR': dict(rate_to_gbp=1)}, {'EUR': fx(1, 'JPY')}):
            self.assertEqual(assess_revenue(profile('10', 'EUR'), income(reportedCurrency='GBP'), rates)['status'], 'missing')

    def test_thresholds_independent_of_decimal_context(self):
        with localcontext() as context:
            context.prec = 3
            self.assertFalse(assess_revenue(profile('10.0000000000000000000001'), income(), {})['passes'])
            self.assertFalse(assess_revenue(profile(), income(grossProfit='29.999999999999999999999'), {})['passes'])

    def test_inputs_not_mutated(self):
        p, i, f = profile(), income(), {}
        original = deepcopy((p, i, f))
        assess_revenue(p, i, f)
        self.assertEqual((p, i, f), original)


class NetCashTests(unittest.TestCase):
    def test_strict_boundary(self):
        self.assertTrue(net_cash(balance())['passes'])
        self.assertFalse(net_cash(balance(cashAndShortTermInvestments='10'))['passes'])
        self.assertFalse(net_cash(balance(cashAndShortTermInvestments='9'))['passes'])
        self.assertTrue(net_cash(balance(cashAndShortTermInvestments='10.000000000000000000001'))['passes'])

    def test_explicit_zero_debt_valid_missing_not_zero(self):
        self.assertTrue(net_cash(balance(totalDebt=0))['passes'])
        for v in (None, '', True, False, 'NaN', 'Infinity', -1, '1e1001'):
            self.assertEqual(net_cash(balance(totalDebt=v))['status'], 'missing')

    def test_combined_used_once_no_lease_addition(self):
        result = net_cash(balance(cashAndCashEquivalents=6, shortTermInvestments=5,
                                 capitalLeaseObligations=100))
        self.assertEqual(result['net_cash'], D(1))
        self.assertEqual(result['cash_source'], 'cashAndShortTermInvestments')
        self.assertEqual(result['balance_currency'], 'USD')
        self.assertTrue(result['passes'])

    def test_missing_combined_requires_both_components(self):
        for combined in (None, ''):
            result = net_cash(balance(cashAndShortTermInvestments=combined,
                                     cashAndCashEquivalents=11, shortTermInvestments=0))
            self.assertTrue(result['passes'])
            self.assertEqual(result['cash_source'], 'cashAndCashEquivalents + shortTermInvestments')
            for field in ('cashAndCashEquivalents', 'shortTermInvestments'):
                b = balance(cashAndShortTermInvestments=combined,
                            cashAndCashEquivalents=11, shortTermInvestments=0)
                del b[field]
                self.assertEqual(net_cash(b)['status'], 'missing')

    def test_bad_combined_not_replaced_by_good_components(self):
        for v in (True, False, 'NaN', 'Infinity', -1, 'bad'):
            self.assertEqual(net_cash(balance(cashAndShortTermInvestments=v,
                cashAndCashEquivalents=6, shortTermInvestments=5))['status'], 'missing')

    def test_invalid_components_never_assumed_zero(self):
        for field in ('cashAndCashEquivalents', 'shortTermInvestments'):
            for v in (None, '', True, 'NaN', -1):
                b = balance(cashAndShortTermInvestments=None, cashAndCashEquivalents=6,
                            shortTermInvestments=5)
                b[field] = v
                self.assertEqual(net_cash(b)['status'], 'missing')

    def test_conflicting_cash_identity_missing(self):
        self.assertEqual(net_cash(balance(cashAndCashEquivalents=20, shortTermInvestments=10))['status'], 'missing')
        result = net_cash(balance(cashAndCashEquivalents=6, shortTermInvestments=4))
        self.assertTrue(result['passes'])
        self.assertEqual(result['cash_reconciliation_difference'], D(1))

    def test_reconciliation_tolerance_does_not_change_net_cash_boundary(self):
        result = net_cash(balance(cashAndShortTermInvestments='10',
                                 cashAndCashEquivalents=6, shortTermInvestments=5))
        self.assertFalse(result['passes'])
        self.assertEqual(result['net_cash'], D(0))

    def test_balance_uses_own_currency_no_fx_needed(self):
        self.assertTrue(net_cash(balance(reportedCurrency='JPY'))['passes'])
        for c in (None, '', 'GBp', 'GBX'):
            self.assertEqual(net_cash(balance(reportedCurrency=c))['status'], 'missing')

    def test_no_cap_or_income_required_for_cash_sign(self):
        self.assertTrue(net_cash(balance(totalStockholdersEquity=-100, freeCashFlow=-10))['passes'])

    def test_bad_record_and_input_immutability(self):
        self.assertEqual(net_cash(None)['status'], 'missing')
        b = balance()
        original = deepcopy(b)
        net_cash(b)
        self.assertEqual(b, original)

    def test_precision_independent_of_context(self):
        with localcontext() as context:
            context.prec = 3
            self.assertTrue(net_cash(balance(cashAndShortTermInvestments='10.000000000000000000001'))['passes'])


if __name__ == '__main__':
    unittest.main()
