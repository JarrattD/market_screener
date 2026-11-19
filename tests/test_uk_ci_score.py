import unittest

from screenerV3.uk_ci_score import RATIO_KEYS, score_company


class TestUKCIScore(unittest.TestCase):
    def setUp(self):
        self.base = dict(
            market_cap=50, current_assets=123, total_liabilities=20,
            minority_interest=3, preferred_equity=0, total_equity=105,
            goodwill_and_intangibles=5, total_debt=10,
            cash_and_short_term_investments=20, annual_fcf=[10, 10, 11, 10, 9],
        )

    def make(self, **changes):
        return score_company(**(self.base | changes))

    def test_full_points_ratios_and_threshold(self):
        outcome = self.make(ownership_pct=20)
        self.assertEqual(outcome["score"], 7)
        self.assertEqual(list(outcome["ratios"]), list(RATIO_KEYS))
        for key, value in zip(RATIO_KEYS, [0.5, 0.5, 5, 4, 5, 4]):
            self.assertEqual(outcome["ratios"][key], value)
        self.assertEqual(set(outcome["ratio_bands"].values()), {"Green"})

    def test_missing_ownership_does_not_block_financial_score(self):
        result = self.make()
        self.assertEqual(result["score"], 6)
        self.assertEqual(result["ownership_status"], "Not found")

    def test_ownership_bands_and_verified_fallback(self):
        for percentage, band in [(20.01, "Yellow"), (49.99, "Yellow"), (50, "Grey")]:
            with self.subTest(percentage=percentage):
                self.assertEqual(self.make(ownership_pct=percentage)["ownership_band"], band)
        self.assertEqual(self.make(free_float_pct=80)["score"], 7)
        self.assertEqual(self.make(ownership_pct=25, free_float_pct=90)["score"], 6)
        self.assertEqual(self.make(ownership_pct=11, ownership_green_eligible=False)["score"], 6)

    def test_ownership_evidence_can_outlive_a_market_data_gap(self):
        result = self.make(market_cap=None, ownership_pct=9)
        self.assertEqual(result["score"], 1)
        self.assertEqual(set(result["ratios"].values()), {None})

    def test_market_check_or_inactive_no_scoring(self):
        for status in ("Inactive", "Check"):
            result = self.make(market_status=status, ownership_pct=9)
            self.assertEqual(result["score"], 0)
            self.assertEqual(set(result["ratio_notes"].values()), {status})

    def test_latest_and_average_dont_fill_missing(self):
        result = self.make(annual_fcf=[12, None, 11, 10, 9])
        self.assertEqual(result["score"], 4)
        self.assertIsNone(result["mean_fcf"])
        self.assertEqual(result["latest_fcf"], 12)
        self.assertIsNone(result["ratios"]["p_fcf_5y"])
        self.assertIsNone(self.make(annual_fcf=[None, 20, 25, 20, 20])["latest_fcf"])

    def test_negative_ev_qualifier_with_positive_negative_and_zero_fcf(self):
        for latest, latest_score, expected_note in [
            (10, 4, "EV negative"), (-10, 2, "FCF negative; EV negative"),
            (0, 2, "FCF zero; EV negative"),
        ]:
            with self.subTest(latest=latest):
                result = self.make(annual_fcf=[latest], cash_and_short_term_investments=80)
                self.assertEqual(result["score"], latest_score)
                self.assertEqual(result["ratio_notes"]["ev_fcf_latest"], expected_note)
        self.assertEqual(self.make(cash_and_short_term_investments=60)["ratios"]["ev_fcf_latest"], 0)

    def test_book_thresholds_zero_denominator_and_negative(self):
        result = self.make(current_assets=73, total_equity=55)
        self.assertEqual(result["ratio_bands"]["p_ncav"], "Orange")
        self.assertEqual(result["ratio_bands"]["p_tbv"], "Orange")
        result = self.make(current_assets=23, total_equity=3)
        self.assertIsNone(result["ratios"]["p_ncav"])
        self.assertEqual(result["ratio_bands"]["p_tbv"], "Red")

    def test_unresolved_preferred_only_blocks_book_ratios(self):
        result = self.make(preferred_equity=20)
        self.assertEqual(result["score"], 4)
        self.assertIsNone(result["ncav"])
        self.assertIsNone(result["tbv"])
        result = self.make(preferred_equity=20, preferred_ncav_treatment="already_included",
                           preferred_tbv_treatment="deduct_raw", equity_includes_minority=True)
        self.assertEqual(result["ncav"], 100)
        self.assertEqual(result["tbv"], 77)

    def test_missing_is_not_zero(self):
        result = self.make(total_debt=None, preferred_equity=None)
        self.assertIsNone(result["ev"])
        self.assertIsNone(result["ncav"])
        self.assertIsNone(result["tbv"])
        self.assertEqual(result["score"], 2)


if __name__ == "__main__":
    unittest.main()
