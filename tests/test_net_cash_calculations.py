import unittest
from decimal import Decimal
from screenerV3.net_cash_calculations import (
    Criteria, MissingData, combined_cash, evaluate, gross_margin, market_cap_gbp,
    tangible_equity)


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.balance = dict(cashAndShortTermInvestments=11, cashAndCashEquivalents=8,
                            shortTermInvestments=3, totalDebt=10, reportedCurrency="GBP",
                            totalStockholdersEquity=100, goodwillAndIntangibleAssets=20,
                            capitalLeaseObligations=2)
        self.income = dict(period="FY", revenue=100, grossProfit=30)

    def check(self, cap=75000000, **kwargs):
        return evaluate(cap, "GBP", self.balance, self.income, {}, **kwargs)

    def test_inclusive_boundaries(self):
        self.assertTrue(self.check()["matches"])
        self.assertFalse(self.check(cap=75000000.01)["matches"])
        self.income["grossProfit"] = 29.999
        self.assertFalse(self.check()["matches"])

    def test_strict_net_cash_and_leases_not_double_counted(self):
        self.assertEqual(self.check()["net_cash"], Decimal(1))
        self.balance["totalDebt"] = 11
        self.assertFalse(self.check()["matches"])

    def test_cash_combined_not_added_twice(self):
        self.assertEqual(combined_cash(self.balance), Decimal(11))
        del self.balance["cashAndShortTermInvestments"]
        self.assertEqual(combined_cash(self.balance), Decimal(11))
        del self.balance["shortTermInvestments"]
        with self.assertRaises(MissingData):
            combined_cash(self.balance)

    def test_missing_debt_not_zero(self):
        self.balance["totalDebt"] = None
        with self.assertRaises(MissingData):
            self.check()

    def test_loss_year_valid_failure(self):
        self.income["grossProfit"] = -5
        self.assertFalse(self.check()["matches"])

    def test_annual_only(self):
        self.income["period"] = "Q4"
        with self.assertRaises(MissingData):
            self.check()

    def test_nonpositive_and_nonfinite_inputs(self):
        for cap in (0, -1, float("nan"), float("inf"), True, None):
            with self.assertRaises(MissingData):
                self.check(cap=cap)
        self.income["revenue"] = 0
        with self.assertRaises(MissingData):
            self.check()

    def test_fx_and_currency_units(self):
        self.assertEqual(market_cap_gbp(100000000, "USD", {"USD": "0.75"}), Decimal(75000000))
        for currency, rates in [("GBp", {}), ("GBX", {}), ("USD", {}), ("USD", {"USD": 0})]:
            with self.assertRaises(MissingData):
                market_cap_gbp(100, currency, rates)

    def test_tbv_attributable_not_total_equity(self):
        self.balance["totalEquity"] = 999
        self.assertEqual(tangible_equity(self.balance), Decimal(80))
        criteria = Criteria(maximum_price_tbv=Decimal(1))
        self.assertTrue(self.check(cap=80, criteria=criteria)["matches"])
        self.assertFalse(self.check(cap=80.01, criteria=criteria)["matches"])
        self.balance["goodwillAndIntangibleAssets"] = 101
        self.assertFalse(self.check(cap=1, criteria=criteria)["matches"])

    def test_no_fcfs_or_country_or_profit_hurdles(self):
        self.income.update(netIncome=-100000, country="CN", freeCashFlow=-100000)
        self.assertTrue(self.check(cap=1)["matches"])

    def test_cheapness_or_any_one_passes(self):
        criteria = Criteria(cheapness_any=True)
        self.assertTrue(self.check(cap=80, criteria=criteria)["matches"])
        self.assertEqual(self.check(cap=80, criteria=criteria)["cheapness_pass_count"], 1)
        flows = [dict(date=f"{year}-12-31", period="FY", reportedCurrency="GBP", freeCashFlow=10)
                 for year in range(2021, 2026)]
        self.balance["totalStockholdersEquity"] = 21  # TBV = 1; TBV route fails.
        value = evaluate(50, "GBP", self.balance, self.income, {}, criteria, flows)
        self.assertTrue(value["matches"])
        self.assertFalse(value["cheapness_routes"]["tbv"])
        self.assertTrue(value["cheapness_routes"]["p_fcf"])
        self.assertTrue(value["cheapness_routes"]["ev_fcf"])
        # P/FCF slightly above 5 but EV/FCF below 5 is sufficient.
        value = evaluate(50.5, "GBP", self.balance, self.income, {}, criteria, flows)
        self.assertTrue(value["matches"])
        self.assertEqual(value["cheapness_pass_count"], 1)

    def test_losses_included_and_missing_fcf_does_not_block_tbv(self):
        criteria = Criteria(cheapness_any=True)
        flows = [dict(date=f"{year}-12-31", period="FY", reportedCurrency="GBP", freeCashFlow=v)
                 for year,v in zip(range(2021,2026),[-10,20,20,20,0])]
        value = evaluate(50, "GBP", self.balance, self.income, {}, criteria, flows)
        self.assertEqual(value["five_year_fcf"], Decimal(10))
        value = evaluate(50, "GBP", self.balance, self.income, {}, criteria, flows[:4])
        self.assertTrue(value["matches"])
        self.assertIsNone(value["cheapness_routes"]["p_fcf"])

    def test_negative_ev_is_valid_but_nonpositive_fcf_is_not(self):
        criteria = Criteria(cheapness_any=True)
        self.balance["cashAndShortTermInvestments"] = 100
        flows = [dict(date=f"{year}-12-31", period="FY", reportedCurrency="GBP", freeCashFlow=10)
                 for year in range(2021,2026)]
        value = evaluate(50, "GBP", self.balance, self.income, {}, criteria, flows)
        self.assertTrue(value["negative_ev"])
        self.assertTrue(value["cheapness_routes"]["ev_fcf"])
        for row in flows: row["freeCashFlow"] = -1
        value = evaluate(50, "GBP", self.balance, self.income, {}, criteria, flows)
        self.assertFalse(value["cheapness_routes"]["ev_fcf"])
        self.assertTrue(value["matches"]) # TBV path alone still sufficient.

    def test_incomplete_fcf_and_no_tbv_pass_unresolved(self):
        self.balance["totalStockholdersEquity"] = 21
        with self.assertRaises(MissingData):
            self.check(cap=50, criteria=Criteria(cheapness_any=True))


if __name__ == "__main__":
    unittest.main()
