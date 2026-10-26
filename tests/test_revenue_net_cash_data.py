"""Offline provider contract and whole-issuer evidence regressions."""
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
from screenerV3.revenue_net_cash_data import FMPClient, issuer_cap_evidence
from screenerV3.negative_ev_provider import FMPError, FMPAuthenticationError

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)

def profile(**changes):
    return dict(dict(symbol='ONE', companyName='One plc', isAdr=False,
        security_eligible=True, quote_verified=True, as_of='2026-10-26T10:00:00Z',
        price=10, cap_amount=1000, currency='USD', cap_currency='USD', cik='001'), **changes)

def shares(**changes):
    return dict(dict(symbol='ONE', outstandingShares=100, date='2026-10-26T09:00:00Z', cik='1'), **changes)

def income(**changes):
    return dict(dict(symbol='ONE', period='FY', date='2025-12-31', reportedCurrency='USD',
        revenue=100, grossProfit=30, acceptedDate='2026-03-01'), **changes)

class EvidenceTests(unittest.TestCase):
    def test_current_non_adr_reconciles_and_binds(self):
        result, reason = issuer_cap_evidence(profile(), [shares()], NOW)
        self.assertIsNone(reason)
        self.assertTrue(result['whole_issuer_cap_verified'])
        self.assertEqual(result['whole_issuer_cap_evidence']['cap_amount'], 1000)

    def test_no_shares_float_fallback(self):
        for value in (None, '', True, -1, 0, float('nan'), float('inf')):
            with self.subTest(value=value):
                result, reason = issuer_cap_evidence(profile(), [shares(outstandingShares=value, floatShares=100)], NOW)
                self.assertFalse(result['whole_issuer_cap_verified'])

    def test_adr_and_classes_unresolved_even_if_numbers_match(self):
        for changes in ({'isAdr': True}, {'isAdr': None}, {'multipleShareClasses': True},
                        {'companyName': 'One Class A'}, {'shareClass': 'A'}):
            self.assertIsNotNone(issuer_cap_evidence(profile(**changes), [shares()], NOW)[1])

    def test_explicit_inactive_unknown_or_quote_failure(self):
        for changes in ({'security_eligible': False}, {'security_eligible': None},
                        {'quote_verified': False}, {'as_of': ''}, {'as_of': '2020-01-01'}):
            self.assertIsNotNone(issuer_cap_evidence(profile(**changes), [shares()], NOW)[1])

    def test_subunit_price_not_double_converted(self):
        result, reason = issuer_cap_evidence(profile(price=1000, currency='GBp', cap_currency='GBP'), [shares()], NOW)
        self.assertIsNone(reason)
        self.assertEqual(result['whole_issuer_cap_evidence']['price_times_shares'], '1000.00')

    def test_one_percent_exact_boundary(self):
        self.assertIsNone(issuer_cap_evidence(profile(price='10.10'), [shares()], NOW)[1])
        self.assertIsNotNone(issuer_cap_evidence(profile(price='10.10001'), [shares()], NOW)[1])

    def test_dates_identity_and_conflicts(self):
        bad = [shares(date='2026-01-01'), shares(date=None), shares(date='2027-01-01'),
               shares(cik='999'), shares(symbol='OTHER')]
        for row in bad:
            self.assertIsNotNone(issuer_cap_evidence(profile(), [row], NOW)[1])
        self.assertIsNotNone(issuer_cap_evidence(profile(), [shares(), shares(outstandingShares=101)], NOW)[1])
        self.assertIsNone(issuer_cap_evidence(profile(), [shares(date='2026-10-25', outstandingShares=999), shares()], NOW)[1])

class FetchTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        return FMPClient(api_key='offline-placeholder', concurrency=2)

    async def test_annual_latest_same_year_and_optional_history(self):
        client = self.client()
        client.statements = AsyncMock(return_value=([income(date='2024-12-31'), income(), income(period='Q2', date='2026-06-30')], None))
        rows, issues = await client.fetch_annual_income(['ONE', 'ONE'])
        self.assertEqual(rows['ONE'].latest['date'], '2025-12-31')
        self.assertFalse(issues)
        client.statements.assert_awaited_once_with('ONE', 'income-statement', 'annual', 8)

    async def test_failed_annual_no_partial_older_fallback(self):
        client = self.client()
        client.statements = AsyncMock(return_value=([income()], 'provider_failed'))
        rows, issues = await client.fetch_annual_income(['ONE'])
        self.assertIsNone(rows['ONE'].latest)
        self.assertTrue(client.health['systemic_failure'])

    async def test_equal_period_conflict_unresolved(self):
        client = self.client()
        client.statements = AsyncMock(return_value=([income(), income(grossProfit=40)], None))
        rows, issues = await client.fetch_annual_income(['ONE'])
        self.assertIsNone(rows['ONE'].latest)

    async def test_balance_frequency_failure_does_not_fall_back(self):
        client = self.client()
        client.statements = AsyncMock(side_effect=[([dict(income(), totalDebt=1, cashAndShortTermInvestments=100)], None), ([], 'failed_interim')])
        rows, issues = await client.fetch_balances(['ONE'])
        self.assertIsNone(rows['ONE'])
        self.assertTrue(issues)

    async def test_shares_endpoint_only_and_denial_no_alternate(self):
        client = self.client()
        client._request = AsyncMock(side_effect=FMPError('endpoint_access_denied', 403))
        rows, issues = await client.verify_issuer_caps({'ONE': profile()})
        client._request.assert_awaited_once_with('/stable/shares-float', {'symbol': 'ONE'})
        self.assertFalse(rows['ONE']['whole_issuer_cap_verified'])
        self.assertTrue(issues)

    async def test_auth_propagates(self):
        client = self.client()
        client._request = AsyncMock(side_effect=FMPAuthenticationError('auth', 401))
        with self.assertRaises(FMPAuthenticationError):
            await client.verify_issuer_caps({'ONE': profile()})

    async def test_verification_never_reuses_old_success_on_failure(self):
        client = self.client()
        client._request = AsyncMock(return_value=[])
        rows, issues = await client.verify_issuer_caps({'ONE': profile(whole_issuer_cap_verified=True, whole_issuer_cap_evidence={'old': 1})})
        self.assertFalse(rows['ONE']['whole_issuer_cap_verified'])
        self.assertEqual(rows['ONE']['whole_issuer_cap_evidence'], {})

if __name__ == '__main__': unittest.main()
