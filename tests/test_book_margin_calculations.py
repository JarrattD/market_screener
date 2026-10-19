"""Deterministic fixtures only: no API, credentials, network or investment data."""
import copy
from dataclasses import dataclass
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
from fractions import Fraction
import random
import unittest

from screenerV3.book_margin_calculations import (
    annual_margin, asset_diagnostics, five_year_margins, screen_book_value,
    screen_market_cap,
)

D = Decimal


def profile(amount="75000000", currency="USD"):
    return {"cap_amount": amount, "cap_currency": currency,
            "cap_currency_basis": "trading_currency"}


def balance(**changes):
    result = {"date": "2025-12-31", "reportedCurrency": "USD",
              "totalStockholdersEquity": "150000000", "totalEquity": "160000000",
              "minorityInterest": "10000000", "preferredStock": "0"}
    result.update(changes)
    return result


def income(**changes):
    result = {"date": "2025-12-31", "fiscalYear": "2025", "period": "FY",
              "reportedCurrency": "USD", "revenue": "100000000", "grossProfit": "30000000",
              "operatingIncome": "-12000000", "ebit": "9000000"}
    result.update(changes)
    return result


def rate(value, currency=None, stamp="2026-10-16T16:00:00+00:00"):
    result = {"rate_to_gbp": value, "as_of": stamp}
    if currency is not None:
        result["currency"] = currency
    return result


FX = {"USD": rate("0.8"), "EUR": rate("1"), "JPY": rate("0.005")}


class MarketCapTests(unittest.TestCase):
    def test_inclusive_usd_boundary_needs_no_fx(self):
        r = screen_market_cap(profile(), {})
        self.assertEqual((r["status"], r["passes"], r["market_cap_usd"]), ("ok", True, D("75000000")))
        self.assertEqual(r["fx_as_of"], {})

    def test_usd_boundary_one_small_decimal_above_fails(self):
        r = screen_market_cap(profile("75000000.00000000000000000000000001"), {})
        self.assertIs(r["passes"], False)
        self.assertEqual(r["status"], "ok")

    def test_usd_boundary_one_small_decimal_below_passes(self):
        self.assertTrue(screen_market_cap(profile("74999999.99999999999999999999999999"), {})["passes"])

    def test_vetted_fields_required_no_raw_fallback(self):
        for p in ({"marketCap": 1, "currency": "USD"}, {"mktCap": 1, "currency": "USD"}, {}):
            with self.subTest(p=p):
                r = screen_market_cap(p, FX)
                self.assertEqual(r["status"], "missing")
                self.assertIsNone(r["passes"])

    def test_missing_invalid_and_nonpositive_cap_never_pass(self):
        for value in (None, "", " ", True, False, "NaN", "Infinity", "-Infinity", float("nan"),
                      "bad", {}, "1e1001", 0, -1, "-0.00"):
            with self.subTest(value=value):
                r = screen_market_cap(profile(value), FX)
                self.assertEqual(r["status"], "missing")
                self.assertIsNone(r["passes"])

    def test_unresolved_currency_rejected(self):
        for currency in (None, "", "US", "USDD", 840, "GBp", "GBX", "ZAc", "ILA"):
            with self.subTest(currency=currency):
                self.assertEqual(screen_market_cap(profile("1", currency), FX)["status"], "missing")
        p = profile("1")
        p["cap_currency_basis"] = "unresolved"
        self.assertEqual(screen_market_cap(p, FX)["status"], "missing")

    def test_price_pence_does_not_rescale_vetted_pound_cap(self):
        p = profile("60000000", "GBP")
        p.update(currency="GBp", price="25", marketCap="6000000000",
                 cap_currency_basis="subunit_reconciled_price_times_shares")
        r = screen_market_cap(p, FX)
        self.assertTrue(r["passes"])
        self.assertEqual(r["market_cap_usd"], D("75000000"))

    def test_fx_cross_not_raw_foreign_amount(self):
        r = screen_market_cap(profile("75000000", "EUR"), FX)
        self.assertFalse(r["passes"])
        self.assertEqual(r["market_cap_usd"], D("93750000"))
        self.assertEqual(r["cap_to_usd_fx_rate"], D("1.25"))

    def test_exact_foreign_boundary_and_dates(self):
        r = screen_market_cap(profile("60000000", "EUR"), FX)
        self.assertTrue(r["passes"])
        self.assertEqual(set(r["fx_as_of"]), {"EUR", "USD"})
        self.assertEqual(r["fx_as_of"]["EUR"], FX["EUR"]["as_of"])
        self.assertFalse(screen_market_cap(profile("60000000.0000000000000000000001", "EUR"), FX)["passes"])

    def test_missing_usd_or_foreign_fx_is_unresolved(self):
        for fx in ({}, {"EUR": rate("1")}, {"USD": rate("0.8")}):
            with self.subTest(fx=fx):
                r = screen_market_cap(profile("1", "EUR"), fx)
                self.assertEqual(r["status"], "missing")
                self.assertIsNone(r["market_cap_usd"])

    def test_invalid_rates_or_dates_are_unresolved(self):
        for invalid in (None, "", "bad", "NaN", "Infinity", 0, -1, True):
            with self.subTest(invalid=invalid):
                self.assertEqual(screen_market_cap(profile("1", "GBP"), {"USD": rate(invalid)})["status"], "missing")
        for stamp in (None, "", 42):
            with self.subTest(stamp=stamp):
                self.assertEqual(screen_market_cap(profile("1", "GBP"), {"USD": rate("0.8", stamp=stamp)})["status"], "missing")

    def test_dataclass_fx_and_case_normalization(self):
        @dataclass
        class FXRate:
            rate_to_gbp: str
            as_of: str
            currency: str
        fx = {"usd": FXRate("0.8", "2026-10-16", "usd")}
        r = screen_market_cap(profile("60000000", " gbp "), fx)
        self.assertTrue(r["passes"])
        self.assertEqual(r["market_cap_currency"], "GBP")

    def test_mislabelled_fx_record_rejected(self):
        r = screen_market_cap(profile("1", "GBP"), {"USD": rate("0.8", currency="EUR")})
        self.assertEqual(r["status"], "missing")
        self.assertIn("fx_currency_mismatch_USD", r["reasons"])

    def test_json_float_uses_decimal_string_not_binary_artifact(self):
        r = screen_market_cap(profile(0.1), {})
        self.assertEqual(r["market_cap_usd"], D("0.1"))


class BookValueTests(unittest.TestCase):
    def test_ordinary_book_boundary_passes_not_tangible_book(self):
        b = balance(goodwill="100000000", intangibleAssets="49000000", totalDebt="900000000")
        r = screen_book_value(profile(), b, {})
        self.assertTrue(r["passes"])
        self.assertEqual(r["price_to_book"], D("0.5"))
        self.assertEqual(r["book_equity"], D("150000000"))
        self.assertEqual(r["equity_basis"], "totalStockholdersEquity - preferredStock")

    def test_boundary_just_above_and_below_are_exact(self):
        for cap, expected in (("75000000.0000000000000000000001", False),
                              ("74999999.9999999999999999999999", True)):
            with self.subTest(cap=cap):
                self.assertIs(screen_book_value(profile(cap), balance(), {})["passes"], expected)

    def test_never_subtract_nci_twice_from_parent_equity(self):
        r = screen_book_value(profile(), balance(), {})
        self.assertEqual(r["book_equity"], D("150000000"))
        self.assertTrue(r["equity_identity_checked"])
        self.assertEqual(r["noncontrolling_interest"], D("10000000"))

    def test_total_equity_fallback_subtracts_nci_once_and_preferred(self):
        b = balance(totalStockholdersEquity=None, totalEquity="180000000", minorityInterest="10000000", preferredStock="20000000")
        r = screen_book_value(profile(), b, {})
        self.assertTrue(r["passes"])
        self.assertEqual(r["book_equity"], D("150000000"))
        self.assertEqual(r["parent_equity"], D("170000000"))
        self.assertEqual(r["preferred_stock_source"], "preferredStock")

    def test_preferred_in_parent_is_subtracted(self):
        b = balance(totalStockholdersEquity="150000000", totalEquity="160000000", preferredStock="20000000")
        r = screen_book_value(profile(), b, {})
        self.assertFalse(r["passes"])
        self.assertEqual(r["book_equity"], D("130000000"))

    def test_explicit_zero_preferred_is_valid(self):
        r = screen_book_value(profile(), balance(preferredStock=0), {})
        self.assertTrue(r["passes"])
        self.assertEqual(r["preferred_stock"], D(0))

    def test_absent_or_invalid_preferred_is_not_silently_zero(self):
        for value in (None, "", "NaN", True, "bad", -1):
            with self.subTest(value=value):
                r = screen_book_value(profile(), balance(preferredStock=value), {})
                self.assertEqual(r["status"], "missing")
                self.assertIsNone(r["passes"])
        b = balance()
        del b["preferredStock"]
        self.assertEqual(screen_book_value(profile(), b, {})["status"], "missing")

    def test_common_equity_is_preferred_explicit_basis(self):
        b = {"commonStockEquity": "150000000", "reportedCurrency": "USD"}
        r = screen_book_value(profile(), b, {})
        self.assertTrue(r["passes"])
        self.assertEqual(r["equity_basis"], "commonStockEquity")
        self.assertIsNone(r["preferred_stock"])

    def test_common_equity_not_reduced_by_preferred_again(self):
        b = balance(commonStockEquity="150000000", totalStockholdersEquity="170000000",
                    totalEquity="180000000", preferredStock="20000000")
        r = screen_book_value(profile(), b, {})
        self.assertTrue(r["passes"])
        self.assertEqual(r["book_equity"], D("150000000"))

    def test_common_equity_contradiction_fails_closed(self):
        r = screen_book_value(profile(), balance(commonStockEquity="140000000"), {})
        self.assertEqual(r["status"], "missing")
        self.assertIn("inconsistent_common_parent_equity_identity", r["reasons"])

    def test_common_equity_reconciles_with_total_minus_nci(self):
        b = balance(commonStockEquity="140000000", totalStockholdersEquity=None)
        r = screen_book_value(profile(), b, {})
        self.assertEqual(r["status"], "missing")
        self.assertIn("inconsistent_common_parent_equity_identity", r["reasons"])

    def test_invalid_explicit_common_equity_never_falls_back(self):
        r = screen_book_value(profile(), balance(commonStockEquity="bad"), {})
        self.assertEqual(r["status"], "missing")

    def test_parent_total_contradiction_fails_closed(self):
        r = screen_book_value(profile(), balance(totalEquity="200000000"), {})
        self.assertEqual(r["status"], "missing")
        self.assertIn("inconsistent_parent_total_equity_identity", r["reasons"])

    def test_parent_total_rounding_tolerance_is_absolute_and_disclosed(self):
        for offset, expected in (("159999999", "ok"), ("160000001", "ok"),
                                 ("160000001.00000001", "missing")):
            with self.subTest(total=offset):
                r = screen_book_value(profile(), balance(totalEquity=offset), {})
                self.assertEqual(r["status"], expected)
                self.assertEqual(r["equity_reconciliation_difference"], D("160000000") - D(offset))
                if expected == "ok":
                    self.assertEqual(r["book_equity"], D("150000000"))

    def test_common_equity_rounding_does_not_change_book_or_gate(self):
        r = screen_book_value(profile(), balance(commonStockEquity="149999999.5"), {})
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["passes"])
        self.assertEqual(r["common_equity_reconciliation_difference"], D("-0.5"))
        self.assertEqual(r["book_equity"], D("149999999.5"))

    def test_nci_missing_does_not_create_zero(self):
        r = screen_book_value(profile(), balance(minorityInterest=None, totalEquity="150000000"), {})
        self.assertTrue(r["passes"])
        self.assertIsNone(r["noncontrolling_interest"])
        self.assertFalse(r["equity_identity_checked"])
        r = screen_book_value(profile(), balance(minorityInterest=None), {})
        self.assertEqual(r["status"], "missing")

    def test_total_only_requires_nci_even_if_preferred_zero(self):
        r = screen_book_value(profile(), balance(totalStockholdersEquity=None, minorityInterest=None), {})
        self.assertEqual(r["status"], "missing")

    def test_parent_only_does_not_need_nci(self):
        b = {"totalStockholdersEquity": "150000000", "preferredStock": 0, "reportedCurrency": "USD"}
        self.assertTrue(screen_book_value(profile(), b, {})["passes"])

    def test_negative_nci_is_not_automatically_invalid(self):
        b = balance(totalEquity="140000000", minorityInterest="-10000000")
        self.assertTrue(screen_book_value(profile(), b, {})["passes"])

    def test_conflicting_nci_aliases_fail_closed(self):
        r = screen_book_value(profile(), balance(noncontrollingInterest="0"), {})
        self.assertEqual(r["status"], "missing")
        self.assertIn("conflicting_noncontrolling_interest", r["reasons"])

    def test_missing_or_invalid_equity_never_passes(self):
        for b in ({}, {"reportedCurrency": "USD", "preferredStock": "0"},
                  balance(totalStockholdersEquity="NaN"), balance(totalEquity=True),
                  balance(minorityInterest="bad")):
            with self.subTest(b=b):
                r = screen_book_value(profile(), b, {})
                self.assertEqual(r["status"], "missing")
                self.assertIsNone(r["passes"])

    def test_known_nonpositive_book_fails_with_no_negative_ratio(self):
        for value in (0, -1, "-500000000"):
            with self.subTest(value=value):
                r = screen_book_value(profile(), {"commonStockEquity": value}, {})
                self.assertEqual((r["status"], r["passes"]), ("ok", False))
                self.assertIsNone(r["price_to_book"])

    def test_missing_book_currency_not_inferred_from_profile(self):
        r = screen_book_value(profile(), balance(reportedCurrency=None), {})
        self.assertEqual(r["status"], "missing")

    def test_same_currency_ratio_does_not_require_unrelated_usd_fx(self):
        r = screen_book_value(profile(currency="EUR"), balance(reportedCurrency="EUR"), {})
        self.assertTrue(r["passes"])
        self.assertIsNone(r["book_equity_usd"])
        self.assertTrue(r["warnings"])

    def test_cross_currency_book_uses_cap_reporting_currency_rate(self):
        r = screen_book_value(profile("75000000", "USD"), balance(reportedCurrency="EUR"), FX)
        self.assertTrue(r["passes"])
        self.assertEqual(r["market_cap_in_book_currency"], D("60000000"))
        self.assertEqual(r["price_to_book"], D("0.4"))
        self.assertEqual(r["book_equity_usd"], D("187500000"))
        self.assertEqual(set(r["cap_to_book_fx_as_of"]), {"USD", "EUR"})

    def test_cross_currency_missing_rate_stays_missing(self):
        r = screen_book_value(profile(), balance(reportedCurrency="EUR"), {})
        self.assertEqual(r["status"], "missing")
        self.assertIsNone(r["price_to_book"])

    def test_reporting_subunits_and_whole_cap_not_confused(self):
        b = {"commonStockEquity": "15000000000", "reportedCurrency": "GBp"}
        r = screen_book_value(profile("75000000", "GBP"), b, FX)
        self.assertTrue(r["passes"])
        self.assertEqual(r["market_cap_in_book_currency"], D("7500000000"))
        self.assertEqual(r["price_to_book"], D("0.5"))

    def test_reporting_subunit_fx_from_raw_or_base_is_equivalent(self):
        b = {"commonStockEquity": "15000000000", "reportedCurrency": "ZAc"}
        p = profile("75000000", "ZAR")
        base_fx = {"USD": rate("0.8"), "ZAR": rate("0.04")}
        raw_fx = {**base_fx, "ZAc": rate("0.0004")}
        for fx in (base_fx, raw_fx):
            with self.subTest(fx=fx):
                r = screen_book_value(p, b, fx)
                self.assertTrue(r["passes"])
                self.assertEqual(r["price_to_book"], D("0.5"))
                self.assertEqual(r["book_equity_usd"], D("7500000"))

    def test_liabilities_not_subtracted_from_already_reported_equity(self):
        r = screen_book_value(profile(), balance(totalLiabilities="900000000000"), {})
        self.assertTrue(r["passes"])
        self.assertEqual(r["book_equity"], D("150000000"))


class AnnualMarginTests(unittest.TestCase):
    def test_inclusive_30_percent_gross_margin(self):
        r = annual_margin(income())
        self.assertEqual((r["status"], r["passes"], r["gross_margin"]), ("ok", True, D("0.3")))

    def test_precision_boundary_exact_not_rounded_ratio(self):
        for gross, expected in (("29.99999999999999999999999999999", False),
                                ("30", True), ("30.00000000000000000000000000001", True)):
            with self.subTest(gross=gross):
                self.assertIs(annual_margin(income(revenue="100", grossProfit=gross))["passes"], expected)

    def test_operating_income_not_ebit(self):
        r = annual_margin(income())
        self.assertEqual(r["operating_income"], D("-12000000"))
        self.assertEqual(r["operating_margin"], D("-0.12"))
        self.assertEqual(r["ebit"], D("9000000"))
        self.assertTrue(r["passes"])

    def test_absent_operating_income_not_filled_from_ebit(self):
        r = annual_margin(income(operatingIncome=None, ebit="900000000"))
        self.assertIsNone(r["operating_income"])
        self.assertIsNone(r["operating_margin"])
        self.assertTrue(r["passes"])

    def test_zero_operating_income_retained(self):
        r = annual_margin(income(operatingIncome=0, ebit=None))
        self.assertEqual(r["operating_margin"], D(0))
        self.assertIsNone(r["ebit"])
        self.assertTrue(r["passes"])

    def test_missing_gross_profit_not_filled_from_ratio_or_cogs(self):
        r = annual_margin(income(grossProfit=None, grossProfitRatio="0.5", costOfRevenue="50000000"))
        self.assertEqual(r["status"], "missing")
        self.assertIsNone(r["gross_margin"])
        self.assertEqual(r["operating_margin"], D("-0.12"))

    def test_missing_and_invalid_revenue_are_unknown_not_failure(self):
        for value in (None, "", True, "NaN", "Infinity", "bad"):
            with self.subTest(value=value):
                r = annual_margin(income(revenue=value))
                self.assertEqual(r["status"], "missing")
                self.assertIsNone(r["passes"])

    def test_annual_alias_is_supported(self):
        self.assertTrue(annual_margin(income(period=" annual "))["passes"])

    def test_nonpositive_revenue_fails_and_ratios_undefined(self):
        for value in (0, -100):
            with self.subTest(value=value):
                r = annual_margin(income(revenue=value))
                self.assertEqual((r["status"], r["passes"]), ("ok", False))
                self.assertIsNone(r["gross_margin"])
                self.assertIsNone(r["operating_margin"])

    def test_negative_and_zero_gross_profit_are_known_failures(self):
        for value in (0, -1):
            with self.subTest(value=value):
                self.assertIs(annual_margin(income(grossProfit=value))["passes"], False)

    def test_no_undocumented_gross_margin_upper_cap(self):
        self.assertTrue(annual_margin(income(revenue="1", grossProfit="2"))["passes"])

    def test_only_explicit_fy_is_annual(self):
        for period in (None, "", "TTM", "Q4", "Q1"):
            with self.subTest(period=period):
                self.assertEqual(annual_margin(income(period=period))["status"], "missing")

    def test_other_requested_exclusions_not_extra_filters(self):
        r = annual_margin(income(freeCashFlow="-999999999999", netIncome="-999999999",
                                 operatingIncome="-999999999999", ebit="-999999999999"))
        self.assertTrue(r["passes"])


class TrendTests(unittest.TestCase):
    def test_short_history_optional_no_screen_gate(self):
        r = five_year_margins([income()])
        self.assertEqual(r["available_years"], 1)
        self.assertIsNone(r["passes"])
        self.assertIsNone(r["years"][0]["passes"])
        self.assertFalse(r["complete_five_year_history"])

    def test_unique_newest_first_at_most_five(self):
        rows = [income(fiscalYear=str(year), date=f"{year}-12-31") for year in (2020, 2023, 2025, 2022, 2021, 2024)]
        rows.append(dict(rows[2]))
        r = five_year_margins(rows)
        self.assertEqual([y["fiscal_year"] for y in r["years"]], [2025, 2024, 2023, 2022, 2021])
        self.assertEqual(r["available_years"], 5)
        self.assertTrue(r["complete_five_year_history"])

    def test_annual_alias_is_included_in_trend(self):
        r = five_year_margins([income(period="ANNUAL")])
        self.assertEqual(r["available_years"], 1)

    def test_gaps_do_not_claim_a_complete_five_year_history(self):
        rows = [income(fiscalYear=str(y), date=f"{y}-12-31") for y in (2025, 2024, 2022, 2021, 2020)]
        r = five_year_margins(rows)
        self.assertEqual(r["available_years"], 5)
        self.assertFalse(r["complete_five_year_history"])
        self.assertIsNone(r["passes"])

    def test_missing_year_does_not_change_known_margins(self):
        r = five_year_margins([income(), income(fiscalYear="2023", date="2023-12-31", grossProfit=None)])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["available_years"], 1)
        self.assertEqual(r["reported_years"], 2)
        self.assertIsNone(r["years"][1]["gross_margin"])
        self.assertIsNone(r["passes"])

    def test_conflicting_duplicate_is_not_cherry_picked(self):
        r = five_year_margins([income(grossProfit="10000000"), income(grossProfit="40000000")])
        self.assertEqual(r["available_years"], 0)
        self.assertEqual(r["reported_years"], 1)
        self.assertEqual(r["years"][0]["status"], "missing")
        self.assertIsNone(r["years"][0]["gross_margin"])

    def test_empty_or_quarterly_history_is_optional_missing(self):
        for rows in ([], [income(period="Q4")]):
            with self.subTest(rows=rows):
                r = five_year_margins(rows)
                self.assertEqual(r["status"], "missing")
                self.assertIsNone(r["passes"])
                self.assertEqual(r["years"], [])

    def test_missing_fiscal_year_not_inferred_from_date(self):
        r = five_year_margins([income(fiscalYear=None)])
        self.assertEqual(r["years"], [])
        self.assertIn("trend_row_missing_fiscal_year", r["reasons"])

    def test_legacy_calendar_year_supported(self):
        r = five_year_margins([income(fiscalYear=None, calendarYear="2025")])
        self.assertEqual(r["years"][0]["fiscal_year"], 2025)


class AssetDiagnosticsTests(unittest.TestCase):
    def test_missing_asset_components_stay_missing(self):
        r = asset_diagnostics({"totalAssets": "100", "cashAndCashEquivalents": "10"})
        self.assertEqual(r["cash"], D(10))
        self.assertIsNone(r["short_term_investments"])
        self.assertIsNone(r["cash_and_short_term_investments"])
        self.assertIsNone(r["inventory"])
        self.assertIsNone(r["inventory_to_total_assets"])
        self.assertIsNone(r["passes"])

    def test_explicit_zero_remains_known_zero(self):
        r = asset_diagnostics({"totalAssets": "100", "inventory": 0, "goodwill": 0})
        self.assertEqual(r["inventory_to_total_assets"], D(0))
        self.assertEqual(r["goodwill"], D(0))
        self.assertNotIn("inventory", r["missing_fields"])

    def test_combined_values_derived_only_from_two_known_components(self):
        r = asset_diagnostics({"cashAndCashEquivalents": "10", "shortTermInvestments": "20",
                               "goodwill": "4", "intangibleAssets": "6", "totalAssets": "100"})
        self.assertEqual(r["cash_and_short_term_investments"], D(30))
        self.assertEqual(r["goodwill_and_intangibles"], D(10))
        self.assertEqual(r["cash_and_short_term_investments_to_total_assets"], D("0.3"))

    def test_explicit_combined_value_keeps_missing_components_missing(self):
        r = asset_diagnostics({"cashAndShortTermInvestments": "10", "totalAssets": "100"})
        self.assertEqual(r["cash_and_short_term_investments"], D(10))
        self.assertIsNone(r["cash"])
        self.assertIsNone(r["short_term_investments"])

    def test_no_ratios_without_positive_total_assets(self):
        for total in (None, 0, -1, "NaN"):
            with self.subTest(total=total):
                r = asset_diagnostics({"totalAssets": total, "inventory": "5"})
                self.assertIsNone(r["inventory_to_total_assets"])
                self.assertEqual(r["inventory"], D(5))

    def test_empty_diagnostics_is_missing_never_failure(self):
        r = asset_diagnostics({})
        self.assertEqual(r["status"], "missing")
        self.assertIsNone(r["passes"])
        self.assertIsNone(r["total_assets"])

    def test_total_liabilities_and_asset_types_reported_not_filtered(self):
        r = asset_diagnostics({"totalAssets": "100", "totalLiabilities": "200",
                               "propertyPlantEquipmentNet": "40", "netReceivables": "20",
                               "otherAssets": "3"})
        self.assertEqual(r["total_liabilities_to_total_assets"], D(2))
        self.assertEqual(r["property_plant_equipment"], D(40))
        self.assertEqual(r["receivables"], D(20))
        self.assertEqual(r["other_assets"], D(3))
        self.assertIsNone(r["passes"])


class PurityAndPrecisionTests(unittest.TestCase):
    def test_seeded_boundaries_match_independent_fraction_oracle(self):
        rng = random.Random(75)
        for case in range(250):
            with self.subTest(case=case):
                cap = D(rng.randrange(1, 10**12)) / D(100)
                equity = D(rng.randrange(1, 10**12)) / D(100)
                source = D(rng.randrange(1, 10000)) / D(100)
                target = D(rng.randrange(1, 10000)) / D(100)
                fx = {"EUR": rate(source), "USD": rate(target)}
                p = profile(cap, "EUR")
                expected_cap = Fraction(cap) * Fraction(source) <= 75_000_000 * Fraction(target)
                self.assertEqual(screen_market_cap(p, fx)["passes"], expected_cap)
                expected_book = 2 * Fraction(cap) * Fraction(source) <= Fraction(equity) * Fraction(target)
                b = {"commonStockEquity": equity, "reportedCurrency": "USD"}
                self.assertEqual(screen_book_value(p, b, fx)["passes"], expected_book)
                gross = D(rng.randrange(-1000, 1000)) / D(100)
                revenue = D(rng.randrange(1, 1000)) / D(100)
                expected_margin = Fraction(gross) >= Fraction(3, 10) * Fraction(revenue)
                self.assertEqual(annual_margin(income(revenue=revenue, grossProfit=gross))["passes"], expected_margin)

    def test_inputs_are_not_mutated(self):
        p, b, i, fx = profile(), balance(), income(), copy.deepcopy(FX)
        original = copy.deepcopy((p, b, i, fx))
        screen_market_cap(p, fx)
        screen_book_value(p, b, fx)
        annual_margin(i)
        five_year_margins([i, i])
        asset_diagnostics(b)
        self.assertEqual((p, b, i, fx), original)

    def test_results_do_not_depend_on_global_decimal_context(self):
        p = profile("75000000.000000000000000000000000000001", "EUR")
        b = balance(reportedCurrency="JPY")
        i = income(revenue="3", grossProfit="1", operatingIncome="1")
        expected = (screen_market_cap(p, FX), screen_book_value(p, b, FX), annual_margin(i))
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            actual = (screen_market_cap(p, FX), screen_book_value(p, b, FX), annual_margin(i))
        self.assertEqual(actual, expected)

    def test_large_exact_subtraction_does_not_round_under_ambient_context(self):
        b = {"totalStockholdersEquity": "150000000.000000000000000000000000000001",
             "preferredStock": "0.000000000000000000000000000002", "reportedCurrency": "USD"}
        with localcontext() as context:
            context.prec = 3
            r = screen_book_value(profile(), b, {})
        self.assertEqual(r["book_equity"], D("149999999.999999999999999999999999999999"))
        self.assertFalse(r["passes"])

    def test_repeating_fx_ratio_cannot_round_boundary_into_pass(self):
        fx = {"USD": rate("3"), "EUR": rate("1")}
        r = screen_market_cap(profile("225000000.000000000000000000000000000000001", "EUR"), fx)
        self.assertFalse(r["passes"])
        r = screen_book_value(profile("225000000.000000000000000000000000000000001", "EUR"), balance(), fx)
        self.assertFalse(r["passes"])


if __name__ == "__main__":
    unittest.main()
