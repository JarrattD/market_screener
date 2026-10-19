"""End-to-end mocked screening and output-isolation regressions."""
import contextlib
import io
import types
import unittest
from decimal import Decimal
from unittest.mock import patch

import negative_ev_fcf_screen as screen
from screenerV3.negative_ev_data import CashflowHistory
from screenerV3.negative_ev_provider import FXRate
from screenerV3 import negative_ev_output as output


def history(values=(10, -20, 10, 10, 10), currency="GBP"):
    return CashflowHistory([
        dict(symbol="ONE", date=f"{y}-12-31", startDate=f"{y}-01-01", period="FY", fiscalYear=y,
             reportedCurrency=currency, freeCashFlow=v)
        for y, v in zip(range(2021, 2026), values)], fetched_at="2026-10-19T00:00:00Z")


class FixtureClient:
    universe_source = "offline synthetic fixture"
    diagnostics = []
    health = {}
    metrics = {}

    def __init__(self, *, cap=50, cash=100, debt=10, flows=None, reporting="GBP", cap_currency="GBP"):
        self.profile = dict(symbol="ONE", companyName="Fixture", country="CN", security_eligible=True,
                            cap_amount=cap, cap_currency=cap_currency, security_identity="ISIN:ONE")
        self.balance = dict(date="2026-06-30", period="Q2", reportedCurrency=reporting,
                            cashAndShortTermInvestments=cash, totalDebt=debt)
        self.history = flows if flows is not None else history()
        self.balance_symbols = self.cashflow_symbols = None

    async def fetch_universe(self): return [{"symbol": "ONE"}]
    async def fetch_profiles(self, universe): return {"ONE": self.profile}, []
    async def fetch_balances(self, symbols):
        self.balance_symbols = symbols
        return {"ONE": self.balance}, []
    async def fetch_fx(self, currencies):
        return {c: FXRate(c, {"USD": .8, "EUR": 1, "GBP": 1}[c], "2026-10-19", "2026-10-19", "fixture")
                for c in currencies if c in ("USD", "EUR", "GBP")}, []
    async def fetch_cashflow_history(self, symbols):
        self.cashflow_symbols = symbols
        return {"ONE": self.history} if symbols else {}, []


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_circuit_breaker_never_creates_completion(self):
        from screenerV3.negative_ev_provider import FMPError
        client = FixtureClient(cash=60)
        original = client.fetch_balances
        async def balances(symbols):
            result = await original(symbols)
            client._fatal = FMPError("rate_limit_retries_exhausted", 429)
            return result
        client.fetch_balances = balances
        with patch.object(screen, "publish") as published:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(FMPError): await screen.screen(client, object())
            published.assert_not_called()

    async def assess(self, client):
        with patch.object(screen, "publish", return_value={"verified": True}) as published:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                receipt = await screen.screen(client, object())
            self.assertNotIn("ONE", stdout.getvalue())
        self.assertEqual(receipt, {"verified": True})
        return published.call_args.args

    async def test_only_two_filters_and_negative_fcf_period_is_retained(self):
        client = FixtureClient(cap=100_000_000, cash=200_000_000)
        args = await self.assess(client)
        results, audit, coverage = args[2:5]
        self.assertEqual(client.balance_symbols, ["ONE"])
        self.assertEqual(client.cashflow_symbols, ["ONE"])
        self.assertEqual(coverage["matched_rows"], 1)
        self.assertEqual(results[1][15], Decimal(20))
        self.assertEqual(results[1][4], Decimal(-99_999_990))
        self.assertEqual(audit[1][1], "match")
        self.assertEqual(len(results[1]), len(screen.HEADERS))

    async def test_ev_zero_is_rejected_before_cashflow(self):
        client = FixtureClient(cash=60)
        args = await self.assess(client)
        self.assertEqual(client.cashflow_symbols, [])
        self.assertEqual(args[4]["matched_rows"], 0)
        self.assertEqual(args[4]["rejected"], 1)

    async def test_fcf_zero_and_negative_fail(self):
        for values in ((-40, 10, 10, 10, 10), (-41, 10, 10, 10, 10)):
            args = await self.assess(FixtureClient(flows=history(values)))
            self.assertEqual(args[4]["matched_rows"], 0)
            self.assertEqual(args[4]["rejected"], 1)

    async def test_missing_cashflow_is_unresolved_not_zero(self):
        client = FixtureClient()
        client.history.annual[-1]["freeCashFlow"] = None
        args = await self.assess(client)
        self.assertEqual(args[4]["unresolved"], 1)
        self.assertEqual(args[4]["status"], "complete_with_unresolved")
        self.assertNotIn("rejected", args[4])

    async def test_fcf_currency_need_not_match_ev_currency(self):
        args = await self.assess(FixtureClient(flows=history(currency="JPY")))
        self.assertEqual(args[4]["matched_rows"], 1)
        self.assertEqual(args[2][1][5], "GBP")
        self.assertEqual(args[2][1][16], "JPY")

    async def test_same_currency_does_not_require_optional_fx(self):
        args = await self.assess(FixtureClient(reporting="XYZ", cap_currency="XYZ"))
        self.assertEqual(args[4]["matched_rows"], 1)

    async def test_cross_currency_ev_uses_explicit_cross_rate(self):
        args = await self.assess(FixtureClient(cap=100, cash=100, debt=10,
                                             reporting="EUR", cap_currency="USD"))
        self.assertEqual(args[2][1][4], Decimal(-10))
        self.assertEqual(args[2][1][13], Decimal("0.8"))

    async def test_missing_cross_fx_is_unresolved(self):
        args = await self.assess(FixtureClient(reporting="XYZ", cap_currency="USD"))
        self.assertEqual(args[4]["unresolved"], 1)
        self.assertEqual(args[4]["matched_rows"], 0)

    async def test_missing_debt_and_negative_debt_are_unresolved(self):
        for debt in (None, -1):
            args = await self.assess(FixtureClient(debt=debt))
            self.assertEqual(args[4]["unresolved"], 1)

    async def test_systemic_failure_never_publishes_complete_zero(self):
        client = FixtureClient()
        client.health = {"systemic_failure": True}
        with patch.object(screen, "publish") as published:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError): await screen.screen(client, object())
            published.assert_not_called()

    async def test_security_exclusion_and_unknown_are_accounted(self):
        for eligible, expected in ((False, "excluded_security"), (None, "unresolved")):
            client = FixtureClient()
            client.profile["security_eligible"] = eligible
            args = await self.assess(client)
            self.assertEqual(args[4][expected], 1)
            self.assertEqual(args[4]["provider_universe"], 1)


class DestinationTests(unittest.TestCase):
    def test_explicit_id_and_exact_title_are_required(self):
        class Client:
            def open_by_key(self, key): return types.SimpleNamespace(id=key, title="Wrong Sheet")
        mock = types.SimpleNamespace(service_account=lambda **kwargs: Client())
        with patch.dict("sys.modules", {"gspread": mock}):
            with self.assertRaises(ValueError): output.open_destination("dummy-path", "")
            with self.assertRaises(ValueError): output.open_destination("dummy-path", "SHEET_ID")
        mock = types.SimpleNamespace(service_account=lambda **kwargs: types.SimpleNamespace(
            open_by_key=lambda key: types.SimpleNamespace(id=key, title=output.TITLE)))
        with patch.dict("sys.modules", {"gspread": mock}):
            self.assertEqual(output.open_destination("dummy-path", "SHEET_ID").id, "SHEET_ID")

    def test_workflow_production_is_pr_guarded_and_queued(self):
        from pathlib import Path
        text = Path(".github/workflows/negative-ev-fcf-screen.yml").read_text()
        active = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
        self.assertIn("workflow_dispatch:", active)
        self.assertIn("timezone: Europe/London", active)
        self.assertIn("cron: '0 3 * * 6'", active)
        self.assertNotIn("  push:", active)
        self.assertNotIn("  issues:", active)
        test_job, production = active.split("  screen:", 1)
        self.assertNotIn("secrets.", test_job)
        self.assertIn("github.event_name != 'pull_request'", production)
        self.assertIn("github.event_name == 'schedule'", production)
        self.assertIn("github.event_name == 'workflow_dispatch'", production)
        self.assertIn("needs: test", production)
        self.assertIn("group: production-screener", production)
        self.assertIn("cancel-in-progress: false", production)
        self.assertIn("queue: max", production)
        self.assertIn("secrets.FMP_KEY", production)
        self.assertIn("secrets.SERVICE_ACCOUNT_JSON", production)
        self.assertIn("secrets.NEGATIVE_EV_SHEET_ID", production)
        self.assertIn('test -n "$NEGATIVE_EV_SHEET_ID"', production)
        self.assertIn("if: always()", production)
        self.assertNotIn("upload-artifact", production)


if __name__ == "__main__":
    unittest.main()
