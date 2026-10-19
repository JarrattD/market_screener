"""Offline data-pipeline regressions. No credentials or network are used."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from screenerV3.negative_ev_provider import (
    FMPClient,
    FMPError,
    latest_statement,
    normalize_profile,
    reconcile_cap_units,
    security_eligibility,
)


def annual(year, value=10):
    return dict(symbol="ONE", date=f"{year}-12-31", period="FY",
                calendarYear=str(year), freeCashFlow=value)


def profile(symbol):
    return dict(symbol=symbol, companyName="Example Inc", currency="USD",
                marketCap=100, isActivelyTrading=True, isEtf=False, isFund=False)


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def text(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return next(self.responses)


class DataPipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_balance_revision_timestamps_are_normalized(self):
        older = dict(symbol="ONE", date="2025-12-31", period="FY", reportedCurrency="USD",
                     acceptedDate="2026-02-01T09:00:00Z", totalDebt=0, cashAndShortTermInvestments=200)
        newer = dict(older, acceptedDate="2026-02-01 10:00:00", cashAndShortTermInvestments=50)
        self.assertEqual(latest_statement([older, newer], "ONE"), newer)
        self.assertEqual(latest_statement([newer, older], "ONE"), newer)

    def test_conflicting_balance_revision_at_same_time_is_unresolved(self):
        one = dict(symbol="ONE", date="2025-12-31", period="FY", reportedCurrency="USD",
                   acceptedDate="2026-02-01T09:00:00Z", totalDebt=0, cashAndShortTermInvestments=200)
        two = dict(one, acceptedDate="2026-02-01 09:00:00", cashAndShortTermInvestments=50)
        self.assertIsNone(latest_statement([one, two], "ONE"))
        self.assertIsNone(latest_statement([two, one], "ONE"))

    def test_conflicting_balance_with_undated_revision_is_unresolved(self):
        one = dict(symbol="ONE", date="2025-12-31", period="FY", reportedCurrency="USD",
                   acceptedDate="2026-02-01T09:00:00Z", totalDebt=0, cashAndShortTermInvestments=200)
        two = dict(one, acceptedDate=None, cashAndShortTermInvestments=50)
        self.assertIsNone(latest_statement([one, two], "ONE"))
        self.assertIsNone(latest_statement([two, one], "ONE"))

    async def test_429_retry_uses_shared_cooldown(self):
        client = FMPClient("dummy-test-key", retries=1)
        client._session = FakeSession([
            FakeResponse(429, "not logged", {"Retry-After": "90"}),
            FakeResponse(200, "[]"),
        ])
        client.limiter.acquire = AsyncMock()
        client.limiter.cooldown = AsyncMock()
        self.assertEqual(await client._request("/stable/profile"), [])
        client.limiter.cooldown.assert_awaited_once_with(90)
        self.assertEqual(client._session.calls, 2)

    async def test_429_exhaustion_opens_circuit_breaker(self):
        client = FMPClient("dummy-test-key", retries=0)
        client._session = FakeSession([FakeResponse(429, "not logged")])
        client.limiter.acquire = AsyncMock()
        for _ in range(2):
            with self.assertRaises(FMPError) as caught:
                await client._request("/stable/profile")
            self.assertEqual(str(caught.exception), "rate_limit_retries_exhausted")
        self.assertEqual(client._session.calls, 1)

    async def test_5xx_retries_without_exposing_response_body(self):
        client = FMPClient("dummy-test-key", retries=1)
        client._session = FakeSession([
            FakeResponse(502, "apikey=DO_NOT_LOG_THIS_FAKE_VALUE"),
            FakeResponse(200, "[]"),
        ])
        client.limiter.acquire = AsyncMock()
        with patch("screenerV3.negative_ev_provider.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await client._request("/stable/profile"), [])
        self.assertEqual(client._session.calls, 2)

    async def test_access_denial_does_not_try_alternate_route(self):
        client = FMPClient("dummy-test-key")
        client._request = AsyncMock(side_effect=FMPError("endpoint_access_denied", 403))
        with self.assertRaises(FMPError):
            await client._compatible("/stable/profile", "/api/v3/profile/ONE",
                                     {"symbol": "ONE"})
        self.assertEqual(client._request.await_count, 1)

    async def test_retired_route_uses_supported_replacement(self):
        client = FMPClient("dummy-test-key")
        client._request = AsyncMock(side_effect=[
            FMPError("legacy_endpoint_retired", 410), [profile("ONE")],
        ])
        result = await client._compatible("/stable/profile", "/api/v3/profile/ONE",
                                          {"symbol": "ONE"})
        self.assertEqual(result[0]["symbol"], "ONE")
        self.assertFalse(client._legacy_available)

    async def test_universe_directory_failure_is_audited(self):
        client = FMPClient("dummy-test-key")
        client._request = AsyncMock(side_effect=[
            FMPError("endpoint_access_denied", 403),
            [dict(symbol="ONE", type="stock")],
        ])
        result = await client.fetch_universe()
        self.assertEqual(len(result), 1)
        self.assertEqual(len(client.diagnostics), 1)
        self.assertEqual(client.diagnostics[0].stage, "universe:/stable/stock-list")

    async def test_partial_profile_batch_is_reconciled_symbol_by_symbol(self):
        client = FMPClient("dummy-test-key", use_profile_bulk=False)

        async def request(path, params=None, **kwargs):
            if path == "/stable/batch-quote":
                return [dict(symbol=s, marketCap=100, price=1,
                             timestamp=int(datetime.now(timezone.utc).timestamp()))
                        for s in params["symbols"].split(",")]
            if path.endswith("/ONE,THREE,TWO"):
                return [profile("ONE")]
            if path.endswith("/TWO"):
                return [profile("TWO")]
            if path.endswith("/THREE"):
                return []
            raise AssertionError(path)

        client._request = request
        result, issues = await client.fetch_profiles(["ONE", "TWO", "THREE"])
        self.assertEqual(set(result), {"ONE", "TWO"})
        self.assertEqual([(item.symbol, item.reason) for item in issues],
                         [("THREE", "missing_profile")])

    async def test_systemic_profile_failure_sets_health_flag(self):
        client = FMPClient("dummy-test-key", use_profile_bulk=False)
        client._request = AsyncMock(side_effect=FMPError("endpoint_access_denied", 403))
        result, issues = await client.fetch_profiles(["ONE", "TWO"])
        self.assertFalse(result)
        self.assertEqual(len(issues), 2)
        self.assertTrue(client.health["systemic_failure"])

    async def test_fx_cross_and_subunits_require_actual_quote_timestamps(self):
        client = FMPClient("dummy-test-key")
        now = int(datetime.now(timezone.utc).timestamp())

        async def request(path, params=None, **kwargs):
            if path == "/api/v3/quotes/forex":
                return [dict(symbol="USDGBP", price=0.8, timestamp=now),
                        dict(symbol="EURUSD", price=1.2, timestamp=now),
                        dict(symbol="BADGBP", price=0.9)]
            return []

        client._request = request
        result, issues = await client.fetch_fx(["GBP", "GBp", "USD", "EUR", "BAD"])
        self.assertEqual(result["GBp"].rate_to_gbp, 0.01)
        self.assertAlmostEqual(result["EUR"].rate_to_gbp, 0.96)
        self.assertEqual([item.symbol for item in issues], ["BAD"])

    def test_lloyds_profile_pence_price_already_whole_gbp_cap(self):
        # Unit-reconciliation regression from the 2026-10-19 observed profile.
        # The quote timestamp is an offline fixture, not a newly fetched quote.
        observed = dict(profile("LLOY.L"), currency="GBp", price=103.25,
                        marketCap=59_198_770_651, outstandingShares=57_335_371_091)
        original = normalize_profile(observed)
        self.assertIsNone(original["cap_amount"])
        quote = dict(price=103.25, sharesOutstanding=57_335_371_091,
                     timestamp=int(datetime.now(timezone.utc).timestamp()))
        result, issue = reconcile_cap_units(original, quote)
        self.assertIsNone(issue)
        self.assertEqual(result["cap_amount"], 59_198_770_651)
        self.assertEqual(result["cap_currency"], "GBP")
        self.assertEqual(result["cap_currency_basis"], "whole_base_reconciled_price_times_shares")
        from screenerV3.negative_ev_calculations import enterprise_value
        ev = enterprise_value(result["cap_amount"], result["cap_currency"],
                              dict(reportedCurrency="GBP", totalDebt=0,
                                   cashAndShortTermInvestments=1_000_000_000))
        self.assertFalse(ev["passes"])
        self.assertEqual(ev["enterprise_value"], 58_198_770_651)

    def test_profile_share_field_alone_does_not_guess_subunit_cap(self):
        observed = normalize_profile(dict(profile("LLOY.L"), currency="GBp", price=103.25,
                       marketCap=59_198_770_651, outstandingShares=57_335_371_091))
        quote_without_current_shares = dict(price=103.25,
                     timestamp=int(datetime.now(timezone.utc).timestamp()))
        result, issue = reconcile_cap_units(observed, quote_without_current_shares)
        self.assertIsNone(result["cap_amount"])
        self.assertEqual(issue.reason, "subunit_cap_needs_current_price_and_shares")

    async def test_gbp_market_cap_is_not_blindly_divided_by_100(self):
        now = int(datetime.now(timezone.utc).timestamp())
        stock_quote = dict(price=100, sharesOutstanding=100, timestamp=now)
        original = normalize_profile(dict(profile("ONE"), currency="GBp"))
        result, issue = reconcile_cap_units(original, stock_quote)
        self.assertIsNone(issue)
        self.assertEqual(result["cap_amount"], 100)
        self.assertEqual(result["cap_currency"], "GBP")
        self.assertTrue(original["security_eligible"])
        original = normalize_profile(dict(profile("ONE"), currency="GBp", marketCap=10000))
        result, issue = reconcile_cap_units(original, stock_quote)
        self.assertIsNone(issue)
        self.assertEqual(result["cap_amount"], 100)
        self.assertFalse(security_eligibility(dict(profile("ONE"), isFund=True))[0])
        self.assertIsNone(security_eligibility(dict(profile("ONE"), isEtf=None))[0])


if __name__ == "__main__":
    unittest.main()

