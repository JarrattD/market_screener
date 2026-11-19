from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import AsyncMock, patch
from screenerV3.negative_ev_provider import FMPError

from uk_ci_ranked import main, make_rows
from uk_ci_universe import HQDecision


class RankingTests(TestCase):
    def test_fmp_failure_does_not_publish_or_leak_response_details(self):
        with (patch("uk_ci_ranked.open_destination"),
              patch("uk_ci_ranked.verify_destination"),
              patch("uk_ci_ranked.read_ownership", return_value={}),
              patch("uk_ci_ranked.build", side_effect=FMPError("sensitive raw response", 429)),
              patch("uk_ci_ranked.publish") as publish,
              patch("sys.stderr") as stderr):
            self.assertEqual(main([]), 1)
        publish.assert_not_called()
        stderr.write.assert_any_call(
            "UK & CI screen stopped: FMP data unavailable (HTTP 429). No results published.")

    def test_run_only_uses_fmp_for_market_data(self):
        item = NS(issuer_key="GB0000000001", isin="GB0000000001", ticker="ONE",
                  company="One", kind="Company", market="LSE (segment not supplied)",
                  source_url="https://example.org", as_of="2026-11-19")
        profile = {"symbol": "ONE.L", "isin": item.isin, "country": "GB",
                   "description": "One is headquartered in London, United Kingdom.", "zip": "SW1A 1AA"}
        inputs = dict(market_cap=50, current_assets=150, total_liabilities=50,
                      minority_interest=0, preferred_equity=0, total_equity=100,
                      goodwill_and_intangibles=0, total_debt=0, cash_and_short_term_investments=0,
                      annual_fcf=[20] * 5)
        with (patch("uk_ci_ranked.FMPClient") as factory,
              patch("uk_ci_ranked.fetch_fmp_universe", new_callable=AsyncMock,
                    return_value=([item], {item.isin: profile})) as universe,
              patch("uk_ci_ranked.fetch_financials", new_callable=AsyncMock,
                    return_value={item.isin: {"score_inputs": inputs}}) as financials,
              patch("uk_ci_ranked.open_destination") as sheets):
            client = factory.return_value.__aenter__.return_value
            client.uk_universe_stats = {"unique_fmp_issuers": 1}
            self.assertEqual(main(["--dry-run"]), 0)
        universe.assert_awaited_once_with(client)
        financials.assert_awaited_once_with(client, {item.isin: profile})
        sheets.assert_not_called()

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
        self.assertEqual(dict(coverage)["FMP issuer universe"], 4)
        self.assertEqual(dict(coverage)["Main Market | listing source date"], "2026-10-31")
        self.assertEqual(fills[0][11], "green")
