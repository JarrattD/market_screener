from datetime import date, timedelta
from decimal import Decimal
import unittest

from screenerV3.negative_ev_calculations import MissingData, cumulative_fcf, enterprise_value


def annuals(first=2020, last=2025, amount='100', currency='USD', explicit=False):
    rows = []
    for year in range(first, last + 1):
        row = dict(date=f'{year}-12-31', freeCashFlow=amount, reportedCurrency=currency,
                   period='FY', fiscalYear=str(year), symbol='TEST')
        if explicit:
            row['startDate'] = f'{year}-01-01'
        rows.append(row)
    return rows


def interim(year, quarter=2, amount='10', basis='ytd', currency='USD', **extra):
    ends = {1: '03-31', 2: '06-30', 3: '09-30', 4: '12-31'}
    return dict(date=f'{year}-{ends[quarter]}', period=f'Q{quarter}',
                reportedCurrency=currency, freeCashFlow=amount, symbol='TEST',
                **({'basis': basis} if basis else {}), **extra)


def ttm(amount='-80', endpoint='2026-06-30', currency='USD', **extra):
    return dict(date=endpoint, reportedCurrency=currency, freeCashFlowTTM=amount,
                symbol='TEST', **extra)


def calc(annual=None, interim_rows=None, ttm_rows=None, **kwargs):
    return cumulative_fcf(annual if annual is not None else annuals(),
                          interim_rows or [], ttm_rows, as_of='2027-03-01', **kwargs)


class EnterpriseValueTests(unittest.TestCase):
    def setUp(self):
        self.balance = dict(reportedCurrency='USD', totalDebt='20',
                            cashAndShortTermInvestments='150')

    def test_ev_components_are_counted_once(self):
        self.balance.update(cashAndCashEquivalents='100', shortTermInvestments='50',
                            shortTermDebt='10', longTermDebt='10', capitalLeaseObligations='99')
        result = enterprise_value('100', 'USD', self.balance)
        self.assertEqual(result['enterprise_value'], Decimal('-30'))
        self.assertTrue(result['passes'])
        self.assertEqual(result['cash_source'], 'cashAndShortTermInvestments')

    def test_zero_is_a_valid_fail_not_missing(self):
        result = enterprise_value('130', 'USD', self.balance)
        self.assertEqual(result['status'], 'ok')
        self.assertIs(result['passes'], False)

    def test_positive_ev_fails(self):
        self.assertFalse(enterprise_value('200', 'USD', self.balance)['passes'])

    def test_missing_debt_is_indeterminate(self):
        del self.balance['totalDebt']
        result = enterprise_value('100', 'USD', self.balance)
        self.assertIsInstance(result, MissingData)
        self.assertIsNone(result['passes'])

    def test_combined_cash_fallback_requires_both_components(self):
        del self.balance['cashAndShortTermInvestments']
        self.balance.update(cashAndCashEquivalents='100')
        self.assertIsNone(enterprise_value('100', 'USD', self.balance)['passes'])
        self.balance['shortTermInvestments'] = '50'
        self.assertEqual(enterprise_value('100', 'USD', self.balance)['enterprise_value'], Decimal('-30'))

    def test_zero_cap_is_invalid_but_zero_debt_cash_are_valid(self):
        result = enterprise_value('0', 'USD', dict(reportedCurrency='USD', totalDebt=0, cashAndShortTermInvestments=100))
        self.assertEqual(result['reason'], 'invalid_market_cap')
        self.assertIsNone(result['passes'])
        result = enterprise_value('100', 'USD', dict(reportedCurrency='USD', totalDebt=0, cashAndShortTermInvestments=0))
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['passes'])

    def test_directional_fx_conversion(self):
        self.balance['reportedCurrency'] = 'EUR'
        result = enterprise_value('100', 'USD', self.balance,
                                  {'from_currency': 'USD', 'to_currency': 'EUR', 'rate': '0.9'})
        self.assertEqual(result['enterprise_value'], Decimal('-40.0'))
        self.assertEqual(result['market_cap'], Decimal('90.0'))
        self.assertEqual(result['currency'], 'EUR')
        self.assertEqual(enterprise_value('100', 'USD', self.balance, {('USD', 'EUR'): '0.9'})['enterprise_value'], Decimal('-40'))

    def test_fx_missing_unlabelled_reversed_or_nonpositive_rejected(self):
        self.balance['reportedCurrency'] = 'EUR'
        bad_rates = [None, '0.9', {'from_currency':'EUR','to_currency':'USD','rate':'1.1'},
                     {('USD','EUR'):'0'}, {('USD','EUR'):'-1'}]
        for rate in bad_rates:
            with self.subTest(rate=rate):
                self.assertIsNone(enterprise_value('100', 'USD', self.balance, rate)['passes'])

    def test_nonfinite_boolean_and_negative_inputs_rejected(self):
        for value in ['NaN', 'Infinity', True, '-1', None]:
            with self.subTest(value=value):
                self.assertIsNone(enterprise_value(value, 'USD', self.balance)['passes'])

    def test_subunit_currency_cannot_silently_be_uppercased(self):
        for currency in ['GBp', 'GBX', 'ZAC', 'ILA']:
            self.assertEqual(enterprise_value('100', currency, self.balance)['reason'], 'unsupported_currency_unit')

    def test_decimal_near_zero(self):
        self.balance.update(totalDebt='0.1', cashAndShortTermInvestments='0.3000000000000000001')
        result = enterprise_value('0.2', 'USD', self.balance)
        self.assertEqual(result['enterprise_value'], Decimal('-0.0000000000000000001'))
        self.assertTrue(result['passes'])

    def test_negative_cash_component_is_not_hidden_by_other_cash(self):
        del self.balance['cashAndShortTermInvestments']
        self.balance.update(cashAndCashEquivalents='-10', shortTermInvestments='100')
        self.assertEqual(enterprise_value('10', 'USD', self.balance)['reason'], 'negative_balance_component')


class FiveYearCashflowTests(unittest.TestCase):
    def test_latest_five_fy_with_negative_years_retained(self):
        rows = annuals()
        rows[-1]['freeCashFlow'] = '-250'
        result = calc(rows)
        self.assertEqual(result['fcf_sum'], Decimal('150'))
        self.assertEqual(result['period_start'], '2021-01-01')
        self.assertEqual(result['period_end'], '2025-12-31')
        self.assertTrue(result['passes'])
        self.assertTrue(result['exact_calendar_60_months'])
        self.assertEqual(len(result['components']), 5)

    def test_five_annual_labels_alone_do_not_prove_start_date(self):
        result = calc(annuals(first=2021))
        self.assertEqual(result['fcf_sum'], Decimal('500'))
        self.assertIsNone(result['period_start'])
        self.assertFalse(result['exact_calendar_60_months'])

    def test_rolling_5fy_minus_old_ytd_plus_latest_ytd(self):
        result = calc(interim_rows=[interim(2021, amount='10'), interim(2026, amount='-200')])
        self.assertEqual(result['fcf_sum'], Decimal('290'))
        self.assertEqual(result['period_start'], '2021-07-01')
        self.assertEqual(result['period_end'], '2026-06-30')
        self.assertFalse(result['fallback'])
        self.assertTrue(result['exact_calendar_60_months'])
        self.assertEqual([r['coefficient'] for r in result['components']], [1,1,1,1,1,-1,1])

    def test_latest_negative_ttm_does_not_fail_positive_total(self):
        result = calc(interim_rows=[interim(2021, amount='10'), interim(2025, amount='20')],
                      ttm_rows=[ttm('-120')])
        self.assertEqual(result['fcf_sum'], Decimal('290'))
        self.assertTrue(result['passes'])
        self.assertTrue(result['ttm_used_for_ytd'])

    def test_ttm_is_not_added_as_sixth_year(self):
        result = calc(interim_rows=[interim(2021, amount='10'), interim(2025, amount='20')],
                      ttm_rows=[ttm('80')])
        # Current YTD = 80 - 100 + 20 = 0; not 500 + 80.
        self.assertEqual(result['fcf_sum'], Decimal('490'))

    def test_ttm_at_new_fiscal_year_end_can_replace_oldest_year(self):
        result = calc(ttm_rows=[ttm('50', '2026-12-31')])
        self.assertEqual(result['fcf_sum'], Decimal('450'))
        self.assertEqual(result['period_start'], '2022-01-01')
        self.assertEqual(result['period_end'], '2026-12-31')

    def test_negative_and_zero_totals_are_valid_failures(self):
        for amount in ['0', '-10']:
            result = calc(annuals(amount=amount))
            self.assertEqual(result['status'], 'ok')
            self.assertIs(result['passes'], False)

    def test_irrelevant_old_null_interim_cannot_flip_known_rolling_fail_to_pass(self):
        rows = [interim(2021, amount='20'), interim(2026, amount='-1000')]
        baseline = calc(interim_rows=rows)
        rows.append(interim(2019, amount=None))
        result = calc(interim_rows=rows)
        self.assertEqual(result['fcf_sum'], Decimal('-520'))
        self.assertEqual(result['method'], baseline['method'])
        self.assertEqual(result['period_end'], '2026-06-30')
        self.assertIs(result['passes'], False)
        self.assertFalse(result['fallback'])

    def test_irrelevant_middle_year_null_interim_does_not_poison_required_ytd(self):
        result = calc(interim_rows=[interim(2021, amount='20'), interim(2026, amount='-1000'),
                                   interim(2023, amount=None)])
        self.assertEqual(result['fcf_sum'], Decimal('-520'))
        self.assertFalse(result['passes'])

    def test_irrelevant_old_null_ttm_does_not_poison_latest_ttm(self):
        result = calc(interim_rows=[interim(2021, amount='20'), interim(2025, amount='20')],
                      ttm_rows=[ttm('-920'), ttm(None, '2019-06-30')])
        self.assertEqual(result['fcf_sum'], Decimal('-520'))
        self.assertFalse(result['passes'])

    def test_required_newest_null_revision_is_not_backfilled_with_old_interim(self):
        original = interim(2026, amount='-1000', acceptedDate='2026-07-01')
        latest = dict(original, freeCashFlow=None, acceptedDate='2026-08-01')
        result = calc(interim_rows=[interim(2021, amount='20'), original, latest])
        self.assertTrue(result['fallback'])
        self.assertTrue(any('missing_fcf' in warning for warning in result['warnings']))
        self.assertEqual(result['latest_available_date'], '2026-06-30')

    def test_missing_latest_completed_fy_cannot_backshift_fallback(self):
        rows = annuals(first=2019, last=2024)
        for newest in [interim(2026, amount='10', fiscalYear=2026),
                       interim(2026, amount=None, fiscalYear=2026)]:
            result = calc(rows, [newest])
            self.assertEqual(result['reason'], 'missing_latest_completed_fiscal_year')
            self.assertIsNone(result['passes'])

    def test_missing_completed_fy_evidenced_by_ttm_is_unresolved(self):
        result = calc(annuals(first=2019,last=2024), [], [ttm('100',fiscalYear=2026)])
        self.assertEqual(result['reason'], 'missing_latest_completed_fiscal_year')
        self.assertIsNone(result['passes'])

    def test_today_alone_does_not_imply_unfiled_annual_is_missing(self):
        result = cumulative_fcf(annuals(first=2019,last=2024), [], as_of='2027-03-01')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['period_end'], '2024-12-31')

    def test_next_fy_interim_missing_ytd_can_still_use_latest_completed_five_fy(self):
        result = calc(annuals(first=2019,last=2024), [interim(2025,amount='10',fiscalYear=2025)])
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['fallback'])
        self.assertEqual(result['period_end'], '2024-12-31')

    def test_newest_horizon_is_not_cherry_picked(self):
        rows = [interim(2021, 1, '10'), interim(2021, 2, '20'),
                interim(2026, 1, '10'), interim(2026, 2, '-1000')]
        result = calc(interim_rows=rows)
        self.assertEqual(result['period_end'], '2026-06-30')
        self.assertEqual(result['fcf_sum'], Decimal('-520'))
        self.assertFalse(result['passes'])

    def test_unavailable_latest_ytd_falls_back_not_to_older_rolling(self):
        rows = [interim(2021, 1, '10'), interim(2026, 1, '10'), interim(2026, 2, '20', basis=None)]
        result = calc(interim_rows=rows)
        self.assertEqual(result['method'], 'last_5_full_fiscal_years')
        self.assertEqual(result['period_end'], '2025-12-31')
        self.assertEqual(result['latest_available_date'], '2026-06-30')
        self.assertTrue(result['fallback'])
        self.assertTrue(result['warnings'])

    def test_unknown_q2_is_not_assumed_discrete(self):
        result = calc(interim_rows=[interim(2021, amount='20'),
                                   interim(2026, 1, '10', basis=None), interim(2026, 2, '20', basis=None)])
        self.assertTrue(result['fallback'])
        self.assertEqual(result['fcf_sum'], Decimal('500'))

    def test_annual_reconciliation_verifies_old_quarters_and_ttm_resolves_latest(self):
        rows = []
        for year in [2021, 2025]:
            rows += [interim(year, q, str(q * 10), basis=None) for q in range(1,5)]
        rows += [interim(2026,1,'-50',basis=None), interim(2026,2,'-100',basis=None)]
        result = calc(interim_rows=rows, ttm_rows=[ttm('-80')])
        self.assertFalse(result['fallback'])
        self.assertEqual(result['fcf_sum'], Decimal('320'))
        self.assertTrue(result['ttm_used_for_ytd'])

    def test_ytd_q4_does_not_prove_ambiguous_q2_is_discrete(self):
        rows = [interim(2021, q, str(value), basis=None)
                for q,value in [(1,10),(2,30),(3,60),(4,100)]]
        rows.append(interim(2026, amount='10'))
        self.assertTrue(calc(interim_rows=rows)['fallback'])

    def test_explicit_quarter_start_distinguishes_q2_from_ytd(self):
        rows = [interim(2021, amount='20'),
                interim(2026,1,'10',basis=None,startDate='2026-01-01'),
                interim(2026,2,'20',basis=None,startDate='2026-04-01')]
        result = calc(interim_rows=rows)
        self.assertEqual(result['fcf_sum'], Decimal('510'))
        self.assertFalse(result['fallback'])

    def test_irregular_interim_start_prevents_rolling(self):
        rows = [interim(2021, amount='20'), interim(2026, amount='10',startDate='2026-02-01')]
        self.assertTrue(calc(interim_rows=rows)['fallback'])

    def test_conflicting_ttm_and_interim_fall_back(self):
        rows = [interim(2021, amount='10'),interim(2025, amount='20'),interim(2026, amount='30')]
        result = calc(interim_rows=rows, ttm_rows=[ttm('999')])
        self.assertTrue(result['fallback'])
        self.assertTrue(any('disagrees' in w for w in result['warnings']))

    def test_fcf_currency_only_needs_internal_consistency(self):
        rows = annuals(currency='EUR')
        result = calc(rows, [interim(2021, currency='EUR'), interim(2026,currency='EUR')])
        self.assertEqual(result['currency'], 'EUR')
        self.assertEqual(result['status'], 'ok')
        rows[-1]['reportedCurrency'] = 'USD'
        result = calc(rows)
        self.assertEqual(result['reason'], 'fcf_currency_mismatch')
        self.assertIsNone(result['passes'])

    def test_interim_currency_mismatch_does_not_convert_fcf(self):
        result = calc(interim_rows=[interim(2021), interim(2026,currency='EUR')])
        self.assertTrue(result['fallback'])
        self.assertEqual(result['currency'], 'USD')

    def test_missing_year_is_missing_not_failed(self):
        rows = annuals(first=2019)
        rows = [r for r in rows if r['fiscalYear'] != '2023']
        result = calc(rows)
        self.assertIsNone(result['passes'])
        self.assertEqual(result['reason'], 'annual_gap_or_irregular_period')

    def test_insufficient_years(self):
        self.assertEqual(calc(annuals(first=2022))['reason'], 'insufficient_annual_history')

    def test_stub_annual_is_rejected(self):
        rows = annuals()
        rows[-1]['startDate'] = '2025-04-01'
        self.assertEqual(calc(rows)['reason'], 'non_full_fiscal_year')

    def test_annual_overlap_is_rejected(self):
        rows = annuals()
        rows[-1]['startDate'] = '2024-12-25'
        self.assertEqual(calc(rows)['reason'], 'annual_gap_or_overlap')

    def test_exact_duplicates_do_not_add_years(self):
        rows = annuals()
        rows.append(dict(rows[-1]))
        self.assertEqual(calc(rows)['fcf_sum'], Decimal('500'))

    def test_latest_dated_restatement_wins(self):
        rows = annuals()
        rows[-1]['acceptedDate'] = '2026-02-01 10:00:00'
        replacement = dict(rows[-1], freeCashFlow='-100', acceptedDate='2026-03-01 10:00:00')
        rows.append(replacement)
        self.assertEqual(calc(rows)['fcf_sum'], Decimal('300'))

    def test_latest_dated_restatement_replaces_old_null_without_poisoning(self):
        rows = annuals()
        rows[-1].update(freeCashFlow=None, acceptedDate='2026-02-01 10:00:00')
        rows.append(dict(rows[-1], freeCashFlow='50', acceptedDate='2026-03-01T10:00:00Z'))
        self.assertEqual(calc(rows)['fcf_sum'], Decimal('450'))

    def test_mixed_timestamp_formats_choose_actual_latest_restatement(self):
        rows = annuals()
        rows[-1].update(acceptedDate='2026-02-01T09:00:00Z')
        rows.append(dict(rows[-1], freeCashFlow='50', acceptedDate='2026-02-01 10:00:00'))
        self.assertEqual(calc(rows)['fcf_sum'], Decimal('450'))

    def test_undated_conflicting_restatement_is_missing(self):
        rows = annuals()
        rows.append(dict(rows[-1], freeCashFlow='-100'))
        self.assertEqual(calc(rows)['reason'], 'ambiguous_restatement')

    def test_asof_prevents_future_filing_lookahead(self):
        rows = annuals()
        rows[-1]['acceptedDate'] = '2026-02-01'
        rows.append(dict(rows[-1], freeCashFlow='-100',acceptedDate='2026-03-01'))
        result = cumulative_fcf(rows, [], as_of=date(2026,2,15))
        self.assertEqual(result['fcf_sum'], Decimal('500'))

    def test_future_period_ignored(self):
        rows = annuals(last=2026)
        result = cumulative_fcf(rows, [], as_of='2026-06-01')
        self.assertEqual(result['period_end'], '2025-12-31')

    def test_irrelevant_old_null_fcf_does_not_poison_latest_window(self):
        rows = annuals(first=2018)
        rows[0]['freeCashFlow'] = None
        rows[2]['freeCashFlow'] = None  # 2020 date is needed only as a boundary anchor.
        result = calc(rows)
        self.assertEqual(result['fcf_sum'], Decimal('500'))
        self.assertEqual(result['period_start'], '2021-01-01')
        self.assertTrue(result['exact_calendar_60_months'])

    def test_required_null_fcf_is_never_backfilled_with_old_year(self):
        rows = annuals(first=2018)
        rows[-2]['freeCashFlow'] = None
        result = calc(rows)
        self.assertEqual(result['reason'], 'missing_fcf')
        self.assertIsNone(result['passes'])

    def test_fcf_missing_not_zero(self):
        rows = annuals()
        rows[-1]['freeCashFlow'] = None
        self.assertEqual(calc(rows)['reason'], 'missing_fcf')

    def test_fcf_derives_from_signed_capex_without_absolute_value(self):
        rows = annuals()
        rows[-1].pop('freeCashFlow')
        rows[-1].update(operatingCashFlow='100',capitalExpenditure='-20')
        self.assertEqual(calc(rows)['fcf_sum'], Decimal('480'))
        rows[-1]['capitalExpenditure'] = '20'
        self.assertEqual(calc(rows)['reason'], 'ambiguous_capex_sign')

    def test_missing_latest_interim_keeps_known_horizon_in_fallback(self):
        result = calc(interim_rows=[interim(2026,amount=None)])
        self.assertTrue(result['fallback'])
        self.assertEqual(result['latest_available_date'], '2026-06-30')
        self.assertTrue(any('missing_fcf' in w for w in result['warnings']))

    def test_symbol_mismatch_is_missing(self):
        self.assertEqual(calc(symbol='OTHER')['reason'], 'symbol_mismatch')

    def test_52_53_week_fiscal_years_never_claim_exact_60_months(self):
        endpoints = ['2020-12-26','2021-12-25','2022-12-31','2023-12-30','2024-12-28','2025-12-27']
        rows = [dict(date=end,freeCashFlow='100',reportedCurrency='USD',period='FY',fiscalYear=2020+i)
                for i,end in enumerate(endpoints)]
        result = calc(rows)
        self.assertEqual(result['fcf_sum'],Decimal('500'))
        self.assertEqual(result['period_start'],'2020-12-27')
        self.assertFalse(result['exact_calendar_60_months'])
        self.assertTrue(result['warnings'])


class TwentyQuarterTests(unittest.TestCase):
    def quarters(self):
        rows = []
        for year in range(2021,2026):
            for q, (start,end) in enumerate([('01-01','03-31'),('04-01','06-30'),('07-01','09-30'),('10-01','12-31')],1):
                rows.append(dict(date=f'{year}-{end}',startDate=f'{year}-{start}',period=f'Q{q}',
                                 freeCashFlow='-5' if q==1 else '10',reportedCurrency='USD'))
        return rows

    def test_twenty_explicit_nonoverlapping_quarters_work_without_annuals(self):
        result = calc([],self.quarters())
        self.assertEqual(result['method'],'rolling_20_verified_quarters')
        self.assertEqual(result['fcf_sum'],Decimal('125'))
        self.assertTrue(result['exact_calendar_60_months'])
        self.assertTrue(result['passes'])

    def test_twenty_quarters_are_independent_of_unavailable_annual_fcf(self):
        quarters = self.quarters()
        for row in quarters:
            row.update(freeCashFlow='10',basis='quarter')
        years = annuals(first=2021,amount='40')
        years[-1]['freeCashFlow'] = None
        result = calc(years,quarters)
        self.assertEqual(result['method'],'rolling_20_verified_quarters')
        self.assertEqual(result['fcf_sum'],Decimal('200'))
        self.assertTrue(result['passes'])
        self.assertTrue(any('Annual comparison unavailable' in warning for warning in result['warnings']))

    def test_unrelated_missing_annual_never_softens_conflicting_annual_restatement(self):
        quarters = self.quarters()
        for row in quarters:
            row.update(freeCashFlow='10',basis='quarter')
        years = annuals(first=2021,amount='40')
        years[0]['freeCashFlow'] = None
        years.append(dict(years[-1],freeCashFlow='999'))
        for ordered in [years, list(reversed(years)), years[1:] + years[:1]]:
            with self.subTest(first_endpoint=ordered[0]['date']):
                result = calc(ordered,quarters)
                self.assertEqual(result['reason'],'ambiguous_restatement')
                self.assertIsNone(result['passes'])

    def test_unavailable_annual_does_not_allow_ignoring_known_annual_contradiction(self):
        years = annuals(first=2021,amount='25')
        years[-1]['freeCashFlow'] = None
        years[0]['freeCashFlow'] = '999'  # No preceding annual start is available.
        result = calc(years,self.quarters())
        self.assertIsNone(result['passes'])
        self.assertEqual(result['reason'],'missing_fcf')

    def test_missing_newer_annual_endpoint_does_not_backshift_verified_quarters(self):
        years = annuals(first=2021,last=2026,amount='25')
        years[-1]['freeCashFlow'] = None
        result = calc(years,self.quarters())
        self.assertIsNone(result['passes'])
        self.assertEqual(result['reason'],'missing_fcf')

    def test_missing_annual_does_not_allow_ignoring_ttm_contradiction(self):
        years = annuals(first=2021,amount='25')
        years[-1]['freeCashFlow'] = None
        result = calc(years,self.quarters(),[ttm('999','2025-12-31')])
        self.assertIsNone(result['passes'])

    def test_missing_middle_annual_can_use_independent_twenty_quarters(self):
        years = annuals(first=2021,amount='25')
        years[2]['freeCashFlow'] = None
        result = calc(years,self.quarters())
        self.assertEqual(result['method'],'rolling_20_verified_quarters')
        self.assertEqual(result['fcf_sum'],Decimal('125'))

    def test_twenty_quarters_establish_exact_start_when_annual_start_is_unknown(self):
        result = calc(annuals(first=2021,amount='25'),self.quarters())
        self.assertEqual(result['method'],'rolling_20_verified_quarters')
        self.assertTrue(result['exact_calendar_60_months'])
        self.assertFalse(result['fallback'])

    def test_quarter_gap_or_overlap_rejected(self):
        for start in ['2022-03-31','2022-04-02']:
            rows = self.quarters()
            rows[5]['startDate'] = start
            self.assertIsNone(calc([],rows)['passes'])

    def test_19_quarters_insufficient(self):
        self.assertIsNone(calc([],self.quarters()[1:])['passes'])

    def test_20_unbounded_quarters_not_assumed_discrete(self):
        rows = self.quarters()
        for row in rows:
            del row['startDate']
        self.assertIsNone(calc([],rows)['passes'])

    def test_quarter_currency_change_rejected(self):
        rows = self.quarters()
        rows[2]['reportedCurrency'] = 'EUR'
        self.assertIsNone(calc([],rows)['passes'])

    def test_quarter_ttm_disagreement_not_ignored(self):
        result = calc([],self.quarters(),[ttm('100','2025-12-31')])
        self.assertIsNone(result['passes'])

    def test_quarter_annual_disagreement_not_ignored(self):
        result = calc(annuals(first=2024,amount='100'),self.quarters())
        self.assertIsNone(result['passes'])


if __name__ == '__main__':
    unittest.main()
