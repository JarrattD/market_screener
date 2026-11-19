from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from uk_ci_ranked import main, make_rows
from uk_ci_universe import HQDecision, OfficialSourceError


class RankingTests(TestCase):
    def test_source_failure_names_the_exchange_without_publishing(self):
        with (patch("uk_ci_ranked.open_destination"),
              patch("uk_ci_ranked.verify_destination"),
              patch("uk_ci_ranked.read_ownership", return_value={}),
              patch("uk_ci_ranked.load_universe",
                    side_effect=OfficialSourceError("Aquis Companies directory", 403)),
              patch("uk_ci_ranked.publish") as publish,
              patch("sys.stderr") as stderr):
            self.assertEqual(main([]), 1)
        publish.assert_not_called()
        stderr.write.assert_any_call(
            "UK & CI screen stopped: Aquis Companies directory: HTTP 403. No results published.")

    def test_seven_at_top_incomplete_kept_hq_pending_visible(self):
        def item(symbol, kind="Company"):
            return NS(isin=symbol, ticker=symbol, company=symbol, market="Main Market",
                      kind=kind, source_url="https://example.org", as_of="2026-10-31")
        listings = [item("FOUR"), item("SEVEN", "Fund"), item("GAP", "REIT"), item("HQ")]
        decisions = {i.isin: HQDecision("in_scope", "GB", "Evidence") for i in listings}
        decisions["HQ"] = HQDecision("review", "", "Unclear")
        financial = dict(market_cap=50, current_assets=150, total_liabilities=50,
                         minority_interest=0, preferred_equity=0, total_equity=100,
                         goodwill_and_intangibles=0, total_debt=0, cash_and_short_term_investments=0,
                         annual_fcf=[20, 20, 20, 20, 20])
        data = {"SEVEN": {"score_inputs": financial, "notes": ["provider shares differ"]},
                "FOUR": {"score_inputs": {**financial, "annual_fcf": [20, None, None, None, None]}}}
        owners = {"SEVEN": {"ticker": "SEVEN", "pct": 10, "as_of": "2026-11-01",
                             "holder": "Holder", "source": "https://example.org"}}
        ranked, review, coverage, fills = make_rows(listings, {}, decisions, data, "now", owners)
        self.assertEqual([r[:3] for r in ranked], [(1, 7, "SEVEN"), (2, 4, "FOUR"), (None, None, "GAP")])
        self.assertIn("provider shares differ", ranked[0][20])
        self.assertEqual(review[0][0], "HQ")
        self.assertEqual(dict(coverage)["Official issuer universe"], 4)
        self.assertEqual(dict(coverage)["Main Market | listing source date"], "2026-10-31")
        self.assertEqual(fills[0][11], "green")
