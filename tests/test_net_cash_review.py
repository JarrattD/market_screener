"""Independent, offline review regressions for the separate screen only."""
import contextlib
import copy
import io
import unittest
from decimal import Decimal
from unittest.mock import patch

import net_cash_screen
from screenerV3.net_cash_calculations import Criteria, MissingData, evaluate
from screenerV3.net_cash_data import Financials, FXRate, Issue, five_year_cashflows, latest_statement


def annual_flows(values=(10, 10, 10, 10, 10)):
    return [dict(symbol="SYNTH", date=f"{year}-12-31", period="FY", fiscalYear=year,
                 reportedCurrency="GBP", freeCashFlow=value)
            for year, value in zip(range(2021, 2026), values)]


def balance(**changes):
    result = dict(date="2026-06-30", reportedCurrency="GBP", totalDebt=10,
                  cashAndShortTermInvestments=11, totalStockholdersEquity=100,
                  goodwillAndIntangibleAssets=20)
    result.update(changes)
    return result


def income(**changes):
    result = dict(date="2025-12-31", period="FY", revenue=100, grossProfit=30)
    result.update(changes)
    return result


class IndependentArithmeticTests(unittest.TestCase):
    def evaluate(self, cap, *, b=None, flows=None, i=None):
        return evaluate(cap, "GBP", b or balance(), i or income(), {},
                        Criteria(cheapness_any=True), flows)

    def test_fcf_pass_is_not_blocked_by_missing_tbv(self):
        b = balance()
        del b["totalStockholdersEquity"]
        result = self.evaluate(50, b=b, flows=annual_flows())
        self.assertTrue(result["matches"])
        self.assertEqual(result["cheapness_routes"], dict(tbv=None, p_fcf=True, ev_fcf=True))

    def test_ev_only_pass_includes_equality_boundary(self):
        result = self.evaluate(51, b=balance(totalStockholdersEquity=20), flows=annual_flows())
        self.assertTrue(result["matches"])
        self.assertEqual(result["cheapness_routes"], dict(tbv=False, p_fcf=False, ev_fcf=True))
        self.assertEqual(result["ev_fcf"], Decimal(5))
        self.assertFalse(self.evaluate(Decimal("51.0001"), b=balance(totalStockholdersEquity=20),
                                       flows=annual_flows())["matches"])

    def test_zero_mean_cannot_pass_either_fcf_route_even_with_negative_ev(self):
        result = self.evaluate(50, b=balance(cashAndShortTermInvestments=100,
                                           totalStockholdersEquity=20),
                               flows=annual_flows((-40, 10, 10, 10, 10)))
        self.assertFalse(result["matches"])
        self.assertTrue(result["negative_ev"])
        self.assertEqual(result["five_year_fcf"], Decimal(0))
        self.assertFalse(result["cheapness_routes"]["p_fcf"])
        self.assertFalse(result["cheapness_routes"]["ev_fcf"])

    def test_cheapness_never_overrides_a_base_condition(self):
        for b, i in ((balance(totalDebt=11), income()), (balance(), income(grossProfit=29))):
            self.assertFalse(self.evaluate(1, b=b, i=i, flows=annual_flows())["matches"])

    def test_cap_and_balance_currency_conversion_is_used_for_both_multiples(self):
        b = balance(reportedCurrency="EUR", totalStockholdersEquity=70,
                    cashAndShortTermInvestments=20)
        f = [dict(row, reportedCurrency="EUR") for row in annual_flows()]
        result = evaluate(50, "USD", b, income(), {"USD":"0.8", "EUR":"0.8"},
                          Criteria(cheapness_any=True), f)
        self.assertEqual(result["market_cap_gbp"], Decimal(40))
        self.assertEqual(result["price_tbv"], Decimal(1))
        self.assertEqual(result["p_fcf"], Decimal(5))
        self.assertEqual(result["ev_fcf"], Decimal(4))
        self.assertEqual(result["cheapness_pass_count"], 3)

    def test_unknown_and_failed_cheapness_are_not_conflated(self):
        with self.assertRaises(MissingData):
            self.evaluate(51, b=balance(totalStockholdersEquity=20))
        self.assertFalse(self.evaluate(52, b=balance(totalStockholdersEquity=20),
                                       flows=annual_flows())["matches"])


class AnnualDataReviewTests(unittest.TestCase):
    def test_latest_statement_ignores_response_order_and_future_periods(self):
        rows = [dict(income(), symbol="SYNTH"),
                dict(income(date="2024-12-31"), symbol="SYNTH"),
                dict(income(date="2099-12-31"), symbol="SYNTH")]
        self.assertEqual(latest_statement(rows, "SYNTH", annual_only=True)["date"], "2025-12-31")

    def test_restatements_are_not_additional_years(self):
        rows = annual_flows()[1:]
        rows.append(dict(rows[-1], acceptedDate="2026-03-01"))
        selected, reason = five_year_cashflows(rows, "SYNTH")
        self.assertEqual(len(selected), 4)
        self.assertNotEqual(reason, "complete_five_consecutive_financial_years")

    def test_missing_year_is_not_five_consecutive_years(self):
        rows = annual_flows()
        rows[0].update(date="2020-12-31", fiscalYear=2020)
        self.assertNotEqual(five_year_cashflows(rows, "SYNTH")[1],
                            "complete_five_consecutive_financial_years")

    def test_explicit_oldest_stub_is_not_a_complete_financial_year(self):
        rows = annual_flows()
        for row in rows:
            row["startDate"] = f"{row['fiscalYear']}-01-01"
        rows[0]["startDate"] = "2021-07-01"
        self.assertNotEqual(five_year_cashflows(rows, "SYNTH")[1],
                            "complete_five_consecutive_financial_years")

    def test_ten_month_transition_is_not_a_complete_financial_year(self):
        rows = annual_flows()
        rows[-1]["date"] = "2025-10-31"
        self.assertNotEqual(five_year_cashflows(rows, "SYNTH")[1],
                            "complete_five_consecutive_financial_years")


class FixtureClient:
    diagnostics = []
    universe_source = "synthetic_offline_fixture"

    def __init__(self):
        self.names = ["SYNTH_TBV", "SYNTH_FCF", "SYNTH_EV", "SYNTH_GM_FAIL",
                      "SYNTH_ZERO_CASH", "SYNTH_BIG", "SYNTH_NO_CAP", "SYNTH_ALIAS"]
        self.requested = None

    async def fetch_universe(self):
        return [dict(symbol=name) for name in self.names]

    async def fetch_profiles(self, universe):
        profiles = {name:dict(symbol=name, security_eligible=True, cap_amount=50,
                             cap_currency="GBP", country="CN", companyName=name,
                             security_identity="SYMBOL:"+name) for name in self.names}
        profiles["SYNTH_BIG"]["cap_amount"] = 75000001
        profiles["SYNTH_NO_CAP"]["cap_amount"] = None
        profiles["SYNTH_TBV"]["security_identity"] = "ISIN:GB0000000000"
        profiles["SYNTH_ALIAS"]["security_identity"] = "ISIN:GB0000000000"
        profiles["SYNTH_EV"]["cap_amount"] = 51
        return profiles, []

    async def fetch_fx(self, currencies):
        return {c: FXRate(c, 1, "2026-10-19", "2026-10-19", "synthetic")
                for c in currencies if c}, []

    async def fetch_financials(self, symbols, *, include_cashflow=False, income_gate=None, balance_gate=None):
        self.requested = set(symbols)
        result = {}
        for name in symbols:
            b, i, f = balance(), income(), []
            if name == "SYNTH_FCF":
                del b["totalStockholdersEquity"]
                f = annual_flows()
            if name == "SYNTH_EV":
                b["totalStockholdersEquity"] = 20
                f = annual_flows()
            if name == "SYNTH_GM_FAIL":
                i["grossProfit"] = 29
            if name == "SYNTH_ZERO_CASH":
                b["totalDebt"] = 11
            result[name] = Financials(i, b, cashflows=f, cashflow_years_complete=bool(f))
        return result, []


class WholeScreenReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_fixture_or_gate_accounting_aliases_and_private_stdout(self):
        client = FixtureClient()
        output = io.StringIO()
        with patch.object(net_cash_screen, "publish", return_value={"verified":True}) as publish:
            with contextlib.redirect_stdout(output):
                receipt = await net_cash_screen.screen(client, object(), Criteria(cheapness_any=True))
        self.assertTrue(receipt["verified"])
        self.assertNotIn("SYNTH_", output.getvalue())
        self.assertNotIn("SYNTH_BIG", client.requested)
        self.assertNotIn("SYNTH_NO_CAP", client.requested)
        args = publish.call_args.args
        results, audit, coverage = args[2:5]
        self.assertEqual(len(results)-1, 3)
        statuses = {row[0]:row[1] for row in audit[1:]}
        self.assertEqual(len(statuses), len(client.names))
        self.assertEqual(statuses["SYNTH_FCF"], "match")
        self.assertEqual(statuses["SYNTH_EV"], "match")
        self.assertEqual(statuses["SYNTH_NO_CAP"], "unresolved")
        self.assertEqual(statuses["SYNTH_GM_FAIL"], "rejected")
        self.assertEqual(statuses["SYNTH_ZERO_CASH"], "rejected")
        self.assertEqual(sum(value == "duplicate_listing" for value in statuses.values()), 1)
        self.assertEqual(coverage["provider_universe"], 8)
        self.assertEqual(coverage["matched_rows"], 3)
        self.assertEqual(coverage["status"], "complete_with_unresolved")


if __name__ == "__main__":
    unittest.main()
