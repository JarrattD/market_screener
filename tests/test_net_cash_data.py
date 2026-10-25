"""Offline data-pipeline regressions. No credentials or network are used."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from screenerV3.net_cash_data import (
    FMPClient,
    FMPError,
    five_year_cashflows,
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
        with patch("screenerV3.net_cash_data.asyncio.sleep", new=AsyncMock()):
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

    async def test_financials_use_newest_balance_and_retain_loss_years(self):
        client = FMPClient("dummy-test-key")

        async def request(path, params=None, **kwargs):
            if "/income-statement/" in path:
                return [annual(2025)]
            if "/balance-sheet-statement/" in path:
                if params["period"] == "quarter":
                    return [dict(annual(2025), period="Q3", date="2025-09-30")]
                return [annual(2025)]
            if "/cash-flow-statement/" in path:
                return [annual(year, -2 if year == 2023 else 10)
                        for year in range(2020, 2026)]
            raise AssertionError(path)

        client._request = request
        result, issues = await client.fetch_financials(["ONE"], include_cashflow=True)
        self.assertFalse(issues)
        self.assertTrue(result["ONE"].cashflow_years_complete)
        self.assertEqual(len(result["ONE"].cashflows), 5)
        self.assertEqual(result["ONE"].cashflows[2]["freeCashFlow"], -2)
        self.assertEqual(result["ONE"].balance["date"], "2025-12-31")
        self.assertTrue(client.health["financial_data_available"])

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

    async def test_partial_annual_periods_and_ttm_are_rejected(self):
        rows = [annual(year) for year in range(2020, 2026)]
        rows[-1]["startDate"] = "2025-03-01"
        self.assertEqual(five_year_cashflows(rows, "ONE")[1],
                         "annual_cashflow_is_partial_period")
        rows = [annual(year) for year in range(2020, 2026)]
        rows[0]["date"] = "2020-11-01"
        self.assertEqual(five_year_cashflows(rows, "ONE")[1],
                         "oldest_cashflow_is_partial_period")
        self.assertIsNone(latest_statement([dict(annual(2025), period="TTM")], "ONE"))

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
