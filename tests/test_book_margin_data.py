"""Offline statement-selection and bounded-fetch regressions for screen five.

No session is opened. Every transport call uses an in-memory response or mock.
"""
import asyncio
import unittest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

from screenerV3.book_margin_data import (
    AnnualIncome, FMPClient, available_statements, latest_statement,
)
from screenerV3.book_margin_provider import FMPAuthenticationError, FMPError

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def income(symbol="ONE", year=2025, **changes):
    row = dict(symbol=symbol, date=f"{year}-12-31", period="FY",
               fiscalYear=year, reportedCurrency="USD", revenue=100,
               grossProfit=30, operatingIncome=-10, ebit=7,
               acceptedDate=f"{year + 1}-03-01T09:00:00Z")
    return dict(row, **changes)


def balance(symbol="ONE", **changes):
    row = dict(symbol=symbol, date="2025-12-31", period="FY",
               reportedCurrency="USD", totalStockholdersEquity=200,
               preferredStock=0, minorityInterest=5, totalEquity=205,
               acceptedDate="2026-03-01T09:00:00Z")
    return dict(row, **changes)


class StatementSelectionTests(unittest.TestCase):
    def test_future_periods_filings_invalid_dates_and_wrong_security_are_ignored(self):
        good = income()
        bad = [income(date="2027-12-31"), income(filingDate="2027-01-01"),
               income(fillingDate="2027-01-01"), income(acceptedDate="2027-01-01"),
               income(date="not-a-date"), income(acceptedDate="not-a-date"),
               income(symbol="OTHER")]
        self.assertEqual(available_statements(bad + [good], "ONE", NOW), [good])

    def test_future_filing_later_today_is_not_yet_public(self):
        future = income(acceptedDate="2026-10-19T13:00:00Z")
        self.assertEqual(available_statements([future], "ONE", NOW), [])

    def test_filing_timezone_is_compared_as_an_instant(self):
        past = income(acceptedDate="2026-10-19T14:00:00+03:00")
        future = income(acceptedDate="2026-10-19T14:00:00+01:00")
        self.assertEqual(available_statements([past, future], "ONE", NOW), [past])

    def test_date_as_of_is_supported_without_requiring_time(self):
        row = income(acceptedDate="2026-10-19")
        self.assertEqual(available_statements([row], "ONE", date(2026, 10, 19)), [row])

    def test_missing_filing_is_retained_for_explicit_freshness_qualification(self):
        row = income(acceptedDate=None)
        self.assertEqual(available_statements([row], "ONE", NOW), [row])

    def test_newest_annual_not_quarter_selected_regardless_of_api_order(self):
        newest = income()
        older = income(year=2024)
        quarter = income(date="2026-06-30", period="Q2")
        for rows in ([older, quarter, newest], [newest, older, quarter]):
            self.assertEqual(latest_statement(rows, "ONE", annual_only=True, as_of=NOW), newest)

    def test_newest_annual_missing_gross_profit_does_not_fall_back(self):
        newest = income(grossProfit=None)
        older = income(year=2024, grossProfit=95)
        self.assertEqual(latest_statement([older, newest], "ONE", True, NOW), newest)

    def test_future_filing_does_not_replace_last_public_annual(self):
        future = income(acceptedDate="2027-01-01")
        previous = income(year=2024)
        self.assertEqual(latest_statement([future, previous], "ONE", True, NOW), previous)

    def test_newest_missing_balance_equity_does_not_fall_back(self):
        newest = balance(date="2026-06-30", period="Q2", totalStockholdersEquity=None,
                         totalEquity=None, acceptedDate="2026-08-01")
        self.assertEqual(latest_statement([balance(), newest], "ONE", as_of=NOW), newest)

    def test_latest_balance_accepts_annual_quarter_and_half_year(self):
        for period in ("Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY", "ANNUAL"):
            with self.subTest(period=period):
                newest = balance(date="2026-06-30", period=period, acceptedDate="2026-08-01")
                self.assertEqual(latest_statement([newest, balance()], "ONE", as_of=NOW), newest)

    def test_newest_restatement_uses_normalized_timestamp(self):
        original = income(acceptedDate="2026-03-01T09:00:00Z", grossProfit=30)
        revised = income(acceptedDate="2026-03-01 10:00:00", grossProfit=20)
        for rows in ([original, revised], [revised, original]):
            self.assertEqual(latest_statement(rows, "ONE", True, NOW), revised)

    def test_equal_timestamp_conflict_is_unresolved_not_older_fallback(self):
        rows = [income(), income(grossProfit=80), income(year=2024)]
        self.assertIsNone(latest_statement(rows, "ONE", True, NOW))
        self.assertIsNone(latest_statement(list(reversed(rows)), "ONE", True, NOW))

    def test_unordered_conflicting_restatement_is_unresolved(self):
        for field in ("revenue", "grossProfit", "reportedCurrency"):
            with self.subTest(field=field):
                value = "EUR" if field == "reportedCurrency" else 999
                rows = [income(), income(acceptedDate=None, **{field: value})]
                self.assertIsNone(latest_statement(rows, "ONE", True, NOW))

    def test_conflicting_optional_income_and_fiscal_labels_do_not_veto_required_fields(self):
        for field in ("operatingIncome", "ebit", "fiscalYear", "calendarYear"):
            with self.subTest(field=field):
                for undated in (False, True):
                    one = income(**{field: 10})
                    two = income(**{field: 20})
                    if undated:
                        two["acceptedDate"] = None
                    result = latest_statement([one, two], "ONE", True, NOW)
                    self.assertIsNotNone(result)
                    self.assertEqual(result["revenue"], 100)
                    self.assertEqual(result["grossProfit"], 30)
                    self.assertIsNone(result.get(field))
                    self.assertIn(field, result.get("_diagnostic_conflicts", []))

    def test_conflicting_optional_asset_composition_is_qualified_not_an_equity_filter(self):
        for field in ("totalAssets", "totalLiabilities", "goodwill", "intangibleAssets",
                      "inventory", "cashAndCashEquivalents", "propertyPlantEquipmentNet"):
            with self.subTest(field=field):
                result = latest_statement([balance(**{field: 10}), balance(**{field: 20})], "ONE", as_of=NOW)
                self.assertIsNotNone(result)
                self.assertEqual(result["totalStockholdersEquity"], 200)
                self.assertIsNone(result.get(field))
                self.assertIn(field, result.get("_diagnostic_conflicts", []))

    def test_numeric_formatting_is_not_a_restatement_conflict(self):
        one = income(revenue=100, grossProfit="30.00")
        two = income(revenue="100.0", grossProfit=30)
        self.assertIsNotNone(latest_statement([one, two], "ONE", True, NOW))

    def test_book_restatement_conflicts_include_all_ordinary_equity_fields(self):
        for field in ("commonStockEquity", "totalStockholdersEquity", "preferredStock",
                      "minorityInterest", "noncontrollingInterest", "totalEquity"):
            with self.subTest(field=field):
                rows = [balance(**{field: 10}), balance(**{field: 20})]
                self.assertIsNone(latest_statement(rows, "ONE", as_of=NOW))

    def test_nonannual_income_only_is_unresolved(self):
        self.assertIsNone(latest_statement([income(period="Q4")], "ONE", True, NOW))


class StatementFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FMPClient("offline-test-key", concurrency=3)
        self.client._compatible = AsyncMock()

    async def test_statement_routes_encode_symbol_and_request_only_needed_fields_endpoint(self):
        self.client._compatible.return_value = [income(symbol="A/B")]
        rows, error = await self.client.statements("A/B", "income-statement", "annual", 8)
        self.assertIsNone(error)
        self.assertEqual(rows[0]["symbol"], "A/B")
        self.client._compatible.assert_awaited_once_with(
            "/stable/income-statement", "/api/v3/income-statement/A%2FB",
            {"symbol": "A/B", "period": "annual", "limit": 8})

    async def test_statement_transport_failure_returns_safe_reason(self):
        self.client._compatible.side_effect = FMPError("network_or_timeout_retries_exhausted")
        rows, error = await self.client.statements("ONE", "income-statement", "annual", 8)
        self.assertEqual(rows, [])
        self.assertEqual(error, "network_or_timeout_retries_exhausted")

    async def test_authentication_and_shared_fatal_circuit_abort(self):
        auth = FMPAuthenticationError("authentication_failed", 401)
        self.client._compatible.side_effect = auth
        with self.assertRaises(FMPAuthenticationError):
            await self.client.statements("ONE", "income-statement", "annual", 8)
        fatal = FMPError("rate_limit_retries_exhausted", 429)
        self.client._fatal = fatal
        self.client._compatible.side_effect = FMPError("unrelated")
        with self.assertRaises(FMPError) as caught:
            await self.client.statements("ONE", "income-statement", "annual", 8)
        self.assertIs(caught.exception, fatal)

    async def test_income_fetch_deduplicates_candidates_without_five_year_gate(self):
        self.client._compatible.return_value = [income()]
        result, issues = await self.client.fetch_annual_income(["ONE", "ONE"])
        self.assertEqual(set(result), {"ONE"})
        self.assertIsInstance(result["ONE"], AnnualIncome)
        self.assertEqual(len(result["ONE"].history), 1)
        self.assertEqual(issues, [])
        self.assertEqual(self.client._compatible.await_count, 1)
        self.assertEqual(self.client.metrics["annual_income_candidates"], 1)
        self.assertTrue(self.client.health["annual_income_available"])
        self.assertFalse(self.client.health["systemic_failure"])

    async def test_empty_candidate_stages_make_no_requests_and_are_not_systemic_failure(self):
        self.assertEqual(await self.client.fetch_annual_income([]), ({}, []))
        self.assertEqual(await self.client.fetch_balances([]), ({}, []))
        self.client._compatible.assert_not_awaited()
        self.assertFalse(self.client.health["systemic_failure"])

    async def test_all_income_unavailable_is_systemic_not_successful_zero_matches(self):
        self.client._compatible.return_value = []
        result, issues = await self.client.fetch_annual_income(["ONE", "TWO"])
        self.assertEqual(set(result), {"ONE", "TWO"})
        self.assertTrue(all(row.latest is None for row in result.values()))
        self.assertEqual({issue.symbol for issue in issues}, {"ONE", "TWO"})
        self.assertTrue(self.client.health["systemic_failure"])

    async def test_partial_income_failure_preserves_good_candidate_and_audits_bad(self):
        async def response(stable, legacy, params):
            if params["symbol"] == "BAD":
                raise FMPError("endpoint_access_denied", 403)
            return [income(symbol=params["symbol"])]
        self.client._compatible.side_effect = response
        result, issues = await self.client.fetch_annual_income(["GOOD", "BAD"])
        self.assertIsNotNone(result["GOOD"].latest)
        self.assertIsNone(result["BAD"].latest)
        self.assertEqual([(issue.symbol, issue.reason) for issue in issues],
                         [("BAD", "endpoint_access_denied")])
        self.assertFalse(self.client.health["systemic_failure"])

    async def test_balance_uses_latest_interim_instead_of_annual(self):
        interim = balance(date="2026-06-30", period="Q2", acceptedDate="2026-08-01")
        async def response(stable, legacy, params):
            return [balance()] if params["period"] == "annual" else [interim]
        self.client._compatible.side_effect = response
        result, issues = await self.client.fetch_balances(["ONE", "ONE"])
        self.assertEqual(result["ONE"], interim)
        self.assertEqual(issues, [])
        self.assertEqual(self.client._compatible.await_count, 2)
        self.assertEqual(self.client.metrics["balance_candidates"], 1)
        self.assertTrue(self.client.health["balance_available"])

    async def test_empty_quarter_is_not_error_and_annual_balance_is_usable(self):
        async def response(stable, legacy, params):
            return [balance()] if params["period"] == "annual" else []
        self.client._compatible.side_effect = response
        result, issues = await self.client.fetch_balances(["ONE"])
        self.assertIsNotNone(result["ONE"])
        self.assertEqual(issues, [])

    async def test_failed_quarter_prevents_silent_fallback_to_stale_annual(self):
        async def response(stable, legacy, params):
            if params["period"] == "quarter":
                raise FMPError("network_or_timeout")
            return [balance()]
        self.client._compatible.side_effect = response
        result, issues = await self.client.fetch_balances(["ONE"])
        self.assertIsNone(result["ONE"])
        self.assertEqual(issues[0].reason, "network_or_timeout")
        self.assertTrue(self.client.health["systemic_failure"])

    async def test_shared_bounded_workers_cover_every_candidate(self):
        running = peak = 0
        async def response(stable, legacy, params):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            running -= 1
            return [income(symbol=params["symbol"])]
        self.client._compatible.side_effect = response
        symbols = [f"T{i:03}" for i in range(53)]
        result, issues = await self.client.fetch_annual_income(symbols)
        self.assertEqual(set(result), set(symbols))
        self.assertEqual(issues, [])
        self.assertLessEqual(peak, self.client.concurrency)
        self.assertEqual(self.client._compatible.await_count, len(symbols))
        # There is one provider transport/request limiter for both statement families.
        self.assertIsInstance(self.client, __import__(
            "screenerV3.book_margin_provider", fromlist=["FMPClient"]).FMPClient)


if __name__ == "__main__":
    unittest.main()
