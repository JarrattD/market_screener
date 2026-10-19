"""Mocked end-to-end screen tests; no FMP, Google, or credential access."""
import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import book_margin_screen as screen
from screenerV3.book_margin_data import AnnualIncome, FMPClient, latest_statement
from screenerV3.book_margin_provider import FXRate, FMPError, Issue
from screenerV3 import book_margin_output as output

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def profile(symbol, **changes):
    return dict(dict(symbol=symbol, companyName=f"Company {symbol}", country="US",
                     exchangeShortName="NYSE", security_eligible=True,
                     security_identity="SYMBOL:" + symbol,
                     cap_amount=50_000_000, cap_currency="USD",
                     cap_currency_basis="trading_currency", as_of=NOW.isoformat(),
                     fetched_at=NOW.isoformat()), **changes)


def income(symbol, **changes):
    return dict(dict(symbol=symbol, date="2025-12-31", period="FY", fiscalYear=2025,
                     revenue=100, grossProfit=30, operatingIncome=-10, ebit=7,
                     reportedCurrency="USD", acceptedDate="2026-03-01T09:00:00Z"), **changes)


def balance(symbol, **changes):
    return dict(dict(symbol=symbol, date="2026-06-30", period="Q2",
                     reportedCurrency="USD", totalStockholdersEquity=100_000_000,
                     preferredStock=0, acceptedDate="2026-08-01T09:00:00Z"), **changes)


def history(row, older=None):
    return AnnualIncome(row, [row] + list(older or []), NOW.isoformat())


class FakeClient:
    """Explicit in-memory provider responses, preserving the real stage contract."""
    def __init__(self, symbols=("ONE",)):
        self.universe = [{"symbol": symbol} for symbol in symbols]
        self.profiles = {symbol: profile(symbol) for symbol in symbols}
        self.incomes = {symbol: history(income(symbol)) for symbol in symbols}
        self.balances = {symbol: balance(symbol) for symbol in symbols}
        self.rates = {}
        self.profile_issues, self.income_issues, self.balance_issues, self.fx_issues = [], [], [], []
        self.diagnostics, self.calls = [], []
        self.health, self.metrics = {}, {"http_requests": 0}
        self.universe_source, self._fatal = "offline-global-universe", None

    async def fetch_universe(self):
        self.calls.append(("universe",))
        return self.universe

    async def fetch_profiles(self, universe):
        self.calls.append(("profiles", tuple(item["symbol"] for item in universe)))
        return self.profiles, list(self.profile_issues)

    async def fetch_fx(self, currencies):
        self.calls.append(("fx", frozenset(currencies)))
        return {key: rate for key, rate in self.rates.items() if key in currencies}, list(self.fx_issues)

    async def fetch_annual_income(self, symbols):
        self.calls.append(("income", tuple(symbols)))
        return {s: self.incomes[s] for s in symbols if s in self.incomes}, list(self.income_issues)

    async def fetch_balances(self, symbols):
        self.calls.append(("balance", tuple(symbols)))
        return {s: self.balances[s] for s in symbols if s in self.balances}, list(self.balance_issues)


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, client):
        captured = {}
        def publish(sheet, batch, rows, audit, coverage, config, records):
            captured.update(rows=rows, audit=audit, coverage=coverage,
                            config=config, records=records, batch=batch, sheet=sheet)
            return {"status": coverage["status"], "verified": True}
        stdout = io.StringIO()
        with patch.object(screen, "publish", side_effect=publish) as mocked, contextlib.redirect_stdout(stdout):
            receipt = await screen.screen(client, "private-sheet-object")
        captured.update(stdout=stdout.getvalue(), receipt=receipt)
        self.assertEqual(mocked.call_count, 1)
        return captured

    def statuses(self, result):
        return {row[0]: row[1] for row in result["audit"][1:] if row[1] != "data_issue"}

    def result_row(self, result, symbol="ONE"):
        return dict(zip(result["rows"][0], next(row for row in result["rows"][1:] if row[0] == symbol)))

    async def test_exact_three_thresholds_pass_and_only_three_gates_are_configured(self):
        client = FakeClient()
        client.profiles["ONE"]["cap_amount"] = 75_000_000
        client.balances["ONE"]["totalStockholdersEquity"] = 150_000_000
        result = await self.execute(client)
        row = self.result_row(result)
        self.assertEqual(row["Market cap USD"], Decimal("75000000"))
        self.assertEqual(row["P/B"], Decimal("0.5"))
        self.assertEqual(row["Latest FY gross margin"], Decimal("0.30"))
        self.assertEqual(self.statuses(result), {"ONE": "match"})
        self.assertEqual(result["config"]["additional_investment_filters"], [])
        self.assertEqual(len(row), len(screen.HEADERS))

    async def test_above_cap_stops_before_income_and_gross_margin_stops_before_balance(self):
        client = FakeClient(("BIG", "LOW", "GOOD"))
        client.profiles["BIG"]["cap_amount"] = "75000000.0000000001"
        client.incomes["LOW"] = history(income("LOW", grossProfit="29.9999999999999999"))
        result = await self.execute(client)
        self.assertEqual(client.calls, [
            ("universe",), ("profiles", ("BIG", "LOW", "GOOD")),
            ("income", ("LOW", "GOOD")), ("balance", ("GOOD",))])
        self.assertEqual(self.statuses(result), {"BIG": "rejected", "LOW": "rejected", "GOOD": "match"})

    async def test_pb_just_over_boundary_rejected_without_rounding(self):
        client = FakeClient()
        client.profiles["ONE"]["cap_amount"] = "50000000.0000000001"
        result = await self.execute(client)
        self.assertEqual(self.statuses(result), {"ONE": "rejected"})
        self.assertEqual(len(result["rows"]), 1)

    async def test_nonpositive_and_unverified_market_caps_are_unresolved(self):
        client = FakeClient(("ZERO", "NEG", "MISSING", "UNITS", "GOOD"))
        for symbol, value in (("ZERO", 0), ("NEG", -1), ("MISSING", None)):
            client.profiles[symbol]["cap_amount"] = value
        client.profiles["UNITS"].update(cap_currency="GBp", cap_currency_basis="unresolved")
        result = await self.execute(client)
        self.assertEqual(self.statuses(result), {
            "ZERO": "unresolved", "NEG": "unresolved", "MISSING": "unresolved",
            "UNITS": "unresolved", "GOOD": "match"})
        self.assertIn(("income", ("GOOD",)), client.calls)

    async def test_no_fcf_net_cash_operating_margin_or_tangible_book_gate(self):
        client = FakeClient()
        client.incomes["ONE"] = history(income("ONE", operatingIncome=-1000, ebit=123,
                                               freeCashFlow=-900_000_000))
        client.balances["ONE"].update(totalDebt=900_000_000, cashAndCashEquivalents=0,
                                     goodwill=200_000_000, intangibleAssets=200_000_000)
        result = await self.execute(client)
        row = self.result_row(result)
        self.assertEqual(self.statuses(result)["ONE"], "match")
        self.assertEqual(row["Operating income"], -1000)
        self.assertEqual(row["Operating margin"], -10)
        self.assertEqual(row["EBIT (separate provider field)"], 123)
        self.assertEqual(row["Ordinary attributable book equity"], 100_000_000)
        self.assertIn("not tangible book", row["Freshness / data qualifications"])
        self.assertEqual(len(json.loads(row["Up to 5 FY gross margins JSON"])["years"]), 1)

    async def test_missing_optional_operating_ebit_assets_and_history_do_not_filter(self):
        client = FakeClient()
        client.incomes["ONE"] = history(income("ONE", operatingIncome=None, ebit=None))
        result = await self.execute(client)
        row = self.result_row(result)
        self.assertEqual(self.statuses(result)["ONE"], "match")
        self.assertIsNone(row["Operating income"])
        self.assertIsNone(row["Operating margin"])
        self.assertIsNone(row["EBIT (separate provider field)"])
        self.assertIsNone(row["Total assets"])

    async def test_missing_latest_gross_profit_is_unresolved_not_old_good_year_fallback(self):
        client = FakeClient(("MISSING", "GOOD"))
        client.incomes["MISSING"] = history(income("MISSING", grossProfit=None),
            [income("MISSING", date="2024-12-31", fiscalYear=2024, grossProfit=99)])
        result = await self.execute(client)
        self.assertEqual(self.statuses(result)["MISSING"], "unresolved")
        self.assertIn(("balance", ("GOOD",)), client.calls)

    async def test_zero_and_negative_revenue_fail_without_division(self):
        client = FakeClient(("ZERO", "NEG"))
        client.incomes["ZERO"] = history(income("ZERO", revenue=0))
        client.incomes["NEG"] = history(income("NEG", revenue=-100))
        result = await self.execute(client)
        self.assertEqual(self.statuses(result), {"ZERO": "rejected", "NEG": "rejected"})
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertIn(("balance", ()), client.calls)

    async def test_nonpositive_book_rejected_missing_book_unresolved(self):
        client = FakeClient(("ZERO", "NEG", "MISSING", "GOOD"))
        for symbol, equity in (("ZERO", 0), ("NEG", -1), ("MISSING", None)):
            client.balances[symbol]["totalStockholdersEquity"] = equity
        result = await self.execute(client)
        self.assertEqual(self.statuses(result), {
            "ZERO": "rejected", "NEG": "rejected", "MISSING": "unresolved", "GOOD": "match"})

    async def test_every_symbol_is_accounted_once_including_excluded_missing_and_duplicate(self):
        symbols = ("ONE", "ALIAS", "DIFFERENT", "FUND", "UNKNOWN", "NO_PROFILE", "BIG", "NO_INCOME", "NO_BALANCE")
        client = FakeClient(symbols)
        client.profiles["ALIAS"]["security_identity"] = client.profiles["ONE"]["security_identity"]
        client.profiles["ONE"]["companyName"] = client.profiles["DIFFERENT"]["companyName"] = "Same issuer"
        client.profiles["ALIAS"]["companyName"] = "Same issuer"
        client.profiles["FUND"].update(security_eligible=False, security_reason="fund_or_etf")
        client.profiles["UNKNOWN"].update(security_eligible=None, security_reason="missing_listing_flags")
        del client.profiles["NO_PROFILE"]
        client.profiles["BIG"]["cap_amount"] = 100_000_000
        del client.incomes["NO_INCOME"]
        del client.balances["NO_BALANCE"]
        result = await self.execute(client)
        status = self.statuses(result)
        self.assertEqual(set(status), set(symbols))
        self.assertEqual(len(result["audit"]) - 1, len(symbols))
        self.assertEqual(status["FUND"], "excluded_security")
        self.assertEqual(status["BIG"], "rejected")
        for symbol in ("UNKNOWN", "NO_PROFILE", "NO_INCOME", "NO_BALANCE"):
            self.assertEqual(status[symbol], "unresolved")
        self.assertEqual(sorted([status["ONE"], status["ALIAS"]]), ["duplicate_listing", "match"])
        self.assertEqual(status["DIFFERENT"], "match")
        self.assertEqual(result["coverage"]["provider_universe"], len(symbols))
        self.assertEqual(result["coverage"]["matched_rows"], 2)
        for row in result["rows"][1:]:
            self.assertTrue(row[screen.HEADERS.index("Possible issuer aliases")])

    async def test_cross_currency_cap_and_book_use_auditable_fx_not_unconverted_values(self):
        client = FakeClient()
        client.profiles["ONE"].update(cap_amount=40_000_000, cap_currency="EUR")
        client.balances["ONE"].update(totalStockholdersEquity=160_000_000, reportedCurrency="CAD")
        client.rates = {currency: FXRate(currency, rate, "2026-10-16T20:00:00Z", NOW.isoformat(), "offline")
                        for currency, rate in (("USD", 0.8), ("EUR", 1), ("CAD", 0.5))}
        result = await self.execute(client)
        row = self.result_row(result)
        self.assertEqual(row["Market cap USD"], 50_000_000)
        self.assertEqual(row["P/B"], Decimal("0.5"))
        self.assertEqual(row["Cap-to-book FX rate"], Decimal(2))
        self.assertEqual([call for call in client.calls if call[0] == "fx"],
                         [("fx", frozenset({"EUR", "USD"})), ("fx", frozenset({"CAD", "USD"}))])
        self.assertEqual({row[0] for row in result["records"][1:] if row[1] == "FX"}, {"USD", "EUR", "CAD"})

    async def test_missing_fx_is_unresolved_never_assumed_parity(self):
        client = FakeClient(("FX", "GOOD"))
        client.profiles["FX"].update(cap_currency="EUR", cap_amount=1)
        client.fx_issues = [Issue("EUR", "fx", "missing_or_stale_fx")]
        result = await self.execute(client)
        self.assertEqual(self.statuses(result), {"FX": "unresolved", "GOOD": "match"})
        self.assertEqual(result["coverage"]["status"], "complete_with_unresolved")
        self.assertIn(("income", ("GOOD",)), client.calls)

    async def test_missing_book_fx_preserves_candidate_as_unresolved(self):
        client = FakeClient()
        client.balances["ONE"]["reportedCurrency"] = "EUR"
        result = await self.execute(client)
        self.assertEqual(self.statuses(result), {"ONE": "unresolved"})
        self.assertEqual(result["coverage"]["matched_rows"], 0)
        self.assertEqual(result["coverage"]["status"], "complete_with_unresolved")

    async def test_annual_alias_is_valid_completed_full_year(self):
        client = FakeClient()
        client.incomes["ONE"] = history(income("ONE", period="ANNUAL"))
        result = await self.execute(client)
        self.assertEqual(self.statuses(result), {"ONE": "match"})

    async def test_successful_zero_results_publish_headers_and_coverage(self):
        client = FakeClient(("BIG",))
        client.profiles["BIG"]["cap_amount"] = 100_000_000
        result = await self.execute(client)
        self.assertEqual(result["rows"], [screen.HEADERS])
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual(result["coverage"]["matched_rows"], 0)
        self.assertIn(("income", ()), client.calls)
        self.assertIn(("balance", ()), client.calls)

    async def test_systemic_failure_or_empty_universe_never_publishes_success(self):
        for mode in ("health", "empty_universe", "no_profiles"):
            with self.subTest(mode=mode):
                client = FakeClient()
                if mode == "health": client.health["systemic_failure"] = True
                if mode == "empty_universe": client.universe = []
                if mode == "no_profiles": client.profiles = {}
                with patch.object(screen, "publish") as publish, contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, "Systemic"):
                        await screen.screen(client, object())
                publish.assert_not_called()

    async def test_shared_fatal_error_aborts_before_next_stage_or_publish(self):
        client = FakeClient()
        async def fetch():
            client._fatal = FMPError("rate_limit_retries_exhausted", 429)
            return client.universe
        client.fetch_universe = fetch
        with patch.object(screen, "publish") as publish, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(FMPError):
                await screen.screen(client, object())
        self.assertEqual(client.calls, [])
        publish.assert_not_called()

    async def test_warnings_and_provider_issues_are_private_not_raw_logs(self):
        symbol = "PRIVATE_CANDIDATE_MARKER"
        client = FakeClient((symbol,))
        client.profiles[symbol].update(companyName="PRIVATE_COMPANY_MARKER", as_of="")
        client.incomes[symbol] = history(income(symbol, date="2020-12-31", fiscalYear=2020, acceptedDate=None))
        client.balances[symbol].update(date="2020-12-31", acceptedDate=None)
        client.diagnostics = [Issue(symbol, "offline-test", "PRIVATE_REASON_MARKER")]
        result = await self.execute(client)
        row = self.result_row(result, symbol)
        self.assertIn("STALE FY", row["Freshness / data qualifications"])
        self.assertIn("STALE BALANCE", row["Freshness / data qualifications"])
        self.assertIn("timestamp unavailable", row["Freshness / data qualifications"])
        self.assertEqual(result["coverage"]["status"], "complete_with_unresolved")
        for private in (symbol, "PRIVATE_COMPANY_MARKER", "PRIVATE_REASON_MARKER", "50000000"):
            self.assertNotIn(private, result["stdout"])
        self.assertTrue(any(row[0] == symbol for row in result["records"][1:]))

    async def test_optional_trend_uses_latest_restatement_and_keeps_originals_in_private_records(self):
        client = FakeClient()
        latest = income("ONE", grossProfit=40, acceptedDate="2026-04-01T09:00:00Z")
        earlier_revision = income("ONE", grossProfit=35, acceptedDate="2026-03-01T09:00:00Z")
        older = [income("ONE", date=f"{year}-12-31", fiscalYear=year,
                        grossProfit=-20, acceptedDate=f"{year+1}-03-01T09:00:00Z")
                 for year in range(2024, 2018, -1)]
        client.incomes["ONE"] = AnnualIncome(latest, [earlier_revision, latest] + older, NOW.isoformat())
        result = await self.execute(client)
        row = self.result_row(result)
        trend = json.loads(row["Up to 5 FY gross margins JSON"])
        self.assertEqual(self.statuses(result)["ONE"], "match")
        self.assertEqual(len(trend["years"]), 5)
        self.assertEqual(trend["years"][0]["fiscal_year"], 2025)
        self.assertEqual(Decimal(trend["years"][0]["gross_margin"]), Decimal("0.4"))
        self.assertTrue(all(year["passes"] is None for year in trend["years"]))
        raw = [json.loads(record[2]) for record in result["records"][1:] if record[1] == "Annual income"]
        self.assertEqual(len(raw), 8)
        self.assertEqual({r["grossProfit"] for r in raw if r["fiscalYear"] == 2025}, {35, 40})

    async def test_run_verifies_destination_and_resumes_before_any_provider_request(self):
        client = FakeClient()
        receipt = {"status": "complete", "already": True}
        with patch.object(screen, "open_destination", return_value="private-sheet") as opened, \
                patch.object(screen, "resume_complete", return_value=receipt), \
                patch.object(screen, "screen", new=AsyncMock()) as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            result = await screen.run("private-account-path", "private-sheet-id", client)
        self.assertIs(result, receipt)
        self.assertEqual(client.calls, [])
        execute.assert_not_awaited()
        opened.assert_called_once_with("private-account-path", "private-sheet-id")

    async def test_optional_income_and_asset_conflicts_still_match_with_visible_qualification(self):
        client = FMPClient("offline-test-key")
        client.fetch_universe = AsyncMock(return_value=[{"symbol": "ONE"}])
        client.fetch_profiles = AsyncMock(return_value=({"ONE": profile("ONE")}, []))
        client.universe_source = "offline-global-universe"
        async def compatible(stable, legacy, params):
            if "income-statement" in stable:
                return [income("ONE", ebit=7, operatingIncome=-10),
                        income("ONE", ebit=8, operatingIncome=-11)]
            if params["period"] == "quarter":
                return [balance("ONE", totalAssets=200_000_000),
                        balance("ONE", totalAssets=300_000_000)]
            return []
        client._compatible = compatible
        result = await self.execute(client)
        row = self.result_row(result)
        self.assertEqual(self.statuses(result), {"ONE": "match"})
        self.assertIsNone(row["EBIT (separate provider field)"])
        self.assertIsNone(row["Operating income"])
        self.assertIsNone(row["Total assets"])
        self.assertIn("conflict", row["Freshness / data qualifications"].lower())
        self.assertIn("ebit", row["Freshness / data qualifications"])
        self.assertIn("totalAssets", row["Freshness / data qualifications"])

    async def test_real_statement_selection_integrates_missing_conflicts_and_future_filings(self):
        client = FMPClient("offline-test-key")
        symbols = ("MISSING", "CONFLICT", "FUTURE", "GOOD")
        client.fetch_universe = AsyncMock(return_value=[{"symbol": s} for s in symbols])
        client.fetch_profiles = AsyncMock(return_value=({s: profile(s) for s in symbols}, []))
        client.universe_source = "offline-global-universe"
        requests = []
        async def compatible(stable, legacy, params):
            requests.append((params["symbol"], stable, params["period"]))
            s = params["symbol"]
            if "income-statement" in stable:
                old = income(s, date="2024-12-31", fiscalYear=2024, grossProfit=99)
                if s == "MISSING": return [old, income(s, grossProfit=None)]
                if s == "CONFLICT": return [old, income(s), income(s, grossProfit=90)]
                if s == "FUTURE": return [income(s, grossProfit=0), income(s, date="2026-12-31")]
                return [income(s)]
            return [balance(s)] if params["period"] == "quarter" else []
        client._compatible = compatible
        result = await self.execute(client)
        self.assertEqual(self.statuses(result), {
            "MISSING": "unresolved", "CONFLICT": "unresolved", "FUTURE": "rejected", "GOOD": "match"})
        balance_requests = [entry for entry in requests if "balance-sheet" in entry[1]]
        self.assertEqual({entry[0] for entry in balance_requests}, {"GOOD"})
        self.assertEqual(len(balance_requests), 2)


class DestinationAndWorkflowTests(unittest.TestCase):
    def test_destination_requires_explicit_id_and_exact_private_screen_title(self):
        def client_for(title, sheet_id):
            sheet = types.SimpleNamespace(title=title, id=sheet_id)
            return types.SimpleNamespace(open_by_key=lambda _: sheet)
        for title, sheet_id, wanted, valid in (
                (output.TITLE, "new-screen-id", "new-screen-id", True),
                ("Old screen", "new-screen-id", "new-screen-id", False),
                (output.TITLE, "other-sheet", "new-screen-id", False),
                (output.TITLE, "new-screen-id", "", False)):
            with self.subTest(title=title, sheet_id=sheet_id, wanted=wanted):
                fake = types.SimpleNamespace(service_account=lambda **kwargs: client_for(title, sheet_id))
                with patch.dict(sys.modules, {"gspread": fake}):
                    if valid:
                        self.assertEqual(output.open_destination("dummy.json", wanted).id, wanted)
                    else:
                        with self.assertRaises(ValueError):
                            output.open_destination("dummy.json", wanted)

    def workflow(self):
        root = Path(__file__).resolve().parents[1]
        return (root / ".github/workflows/book-margin-screen.yml").read_text()

    def test_workflow_scheduled_london_saturday_and_production_main_only(self):
        workflow = self.workflow()
        self.assertIn("cron: '0 3 * * 6'", workflow)
        self.assertIn("timezone: Europe/London", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("github.event_name != 'pull_request'", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("needs: test", workflow)

    def test_workflow_shares_fmp_budget_and_does_not_cancel_running_screen(self):
        workflow = self.workflow()
        self.assertIn("group: production-screener", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("queue: max", workflow)
        self.assertEqual(workflow.count("secrets.FMP_KEY"), 1)
        self.assertIn("secrets.BOOK_MARGIN_SHEET_ID", workflow)
        self.assertNotRegex(workflow, r"BOOK_MARGIN_SHEET_ID: [A-Za-z0-9_-]{20,}")
        self.assertNotIn("FMP_KEY_2", workflow)
        self.assertNotIn("FMP_SECONDARY", workflow)

    def test_workflow_tests_have_no_secrets_and_private_results_are_not_artifacts(self):
        workflow = self.workflow()
        test_job = workflow.split("  test:", 1)[1].split("  screen:", 1)[0]
        self.assertNotIn("secrets.", test_job)
        self.assertIn("test_book_margin*.py", test_job)
        self.assertIn("test_completion.py", test_job)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("GITHUB_STEP_SUMMARY", workflow)
        self.assertIn("umask 077", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("rm -f private_screen_run/service_account.json", workflow)


class CLITests(unittest.TestCase):
    def test_failure_logs_exception_class_only_not_sensitive_error_text(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        async def failed(*args):
            raise RuntimeError("apikey=FAKE_SECRET PRIVATE_CANDIDATE")
        with patch.object(screen, "run", side_effect=failed), \
                patch("sys.argv", ["book_margin_screen.py", "--service-account", "dummy.json", "--spreadsheet-id", "dummy"]), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            status = screen.main()
        self.assertEqual(status, 1)
        self.assertIn("RuntimeError", stderr.getvalue())
        self.assertNotIn("FAKE_SECRET", stderr.getvalue() + stdout.getvalue())
        self.assertNotIn("PRIVATE_CANDIDATE", stderr.getvalue() + stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
