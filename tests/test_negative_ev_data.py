"""Offline requests: no market data, credentials or Google writes."""
import unittest
from unittest.mock import AsyncMock
from screenerV3.negative_ev_data import FMPClient, available_statements
from screenerV3.negative_ev_provider import FMPError


class BalanceFirstTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_circuit_breaker_after_partial_success_propagates(self):
        client = FMPClient("dummy", concurrency=1)
        async def request(path, params=None, **kwargs):
            if path.endswith("/B"):
                client._fatal = FMPError("rate_limit_retries_exhausted", 429)
                raise client._fatal
            return [dict(symbol="A", date="2025-12-31", period="FY")]
        client._request = request
        with self.assertRaises(FMPError):
            await client.fetch_balances(["A", "B"])

    async def test_balance_only_does_not_request_income_or_cashflows(self):
        client = FMPClient("dummy")
        calls = []
        async def request(path, params=None, **kwargs):
            calls.append((path, params))
            return [dict(symbol="ONE", date="2025-12-31", period="FY")] if params["period"] == "annual" else [
                dict(symbol="ONE", date="2026-06-30", period="Q2")]
        client._request = request
        result, issues = await client.fetch_balances(["ONE"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("balance-sheet" in path for path, _ in calls))
        self.assertEqual(result["ONE"]["date"], "2026-06-30")
        self.assertFalse(issues)

    async def test_failed_frequency_is_not_treated_as_empty_or_stale_pass(self):
        client = FMPClient("dummy")
        async def request(path, params=None, **kwargs):
            if params["period"] == "quarter":
                raise FMPError("provider_unavailable", 500)
            return [dict(symbol="ONE", date="2025-12-31", period="FY")]
        client._request = request
        result, issues = await client.fetch_balances(["ONE"])
        self.assertIsNone(result["ONE"])
        self.assertTrue(issues)
        self.assertTrue(client.health["systemic_failure"])

    async def test_successfully_empty_interim_can_use_latest_annual(self):
        client = FMPClient("dummy")
        async def request(path, params=None, **kwargs):
            return [] if params["period"] == "quarter" else [dict(symbol="ONE", date="2025-12-31", period="FY")]
        client._request = request
        result, issues = await client.fetch_balances(["ONE"])
        self.assertEqual(result["ONE"]["date"], "2025-12-31")
        self.assertFalse(issues)

    async def test_history_fetches_boundaries_and_optional_ttm_once(self):
        client = FMPClient("dummy")
        calls = []
        async def request(path, params=None, **kwargs):
            calls.append((path, params))
            if path.endswith("cash-flow-statement-ttm"):
                raise FMPError("endpoint_access_denied", 403)
            return [dict(symbol="ONE", date="2025-12-31", period="FY", freeCashFlow=-7)]
        client._request = request
        result, issues = await client.fetch_cashflow_history(["ONE"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(sorted(p["limit"] for _, p in calls), [8, 8, 32])
        self.assertEqual(result["ONE"].annual[0]["freeCashFlow"], -7)
        self.assertEqual(result["ONE"].errors["ttm"], "endpoint_access_denied")
        self.assertFalse(client.health.get("systemic_failure"))
        self.assertEqual(len(issues), 1)

    async def test_all_cashflow_failure_cannot_publish_complete_zero(self):
        client = FMPClient("dummy")
        client._request = AsyncMock(side_effect=FMPError("provider_unavailable", 500))
        await client.fetch_cashflow_history(["ONE"])
        self.assertTrue(client.health["systemic_failure"])

    def test_future_filing_and_future_period_are_ignored(self):
        rows = [dict(date="2025-12-31", acceptedDate="2099-01-01"),
                dict(date="2099-01-01"), dict(date="2025-12-31", filingDate="2026-01-15")]
        self.assertEqual(available_statements(rows), rows[-1:])


if __name__ == "__main__":
    unittest.main()
