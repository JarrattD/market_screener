import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from uk_ci_universe import (
    Listing, _check_date, _deduplicate, _lse_list, _parse_aquis, classify_hq,
    load_hq_overrides,
)


class UKCIUniverseTests(unittest.TestCase):
    def test_official_lse_selection_excludes_etf_and_trade_only(self):
        headers = ("TIDM", "Issuer Name", "ISIN", "LSE Market", "MiFIR Identifier Code",
                   "FCA Listing Category", "Instrument Name", "Country of Incorporation")
        rows = [
            headers,
            ("C", "Ordinary PLC", "GB0000000010", "AIM", "SHRS", "", "ORD", "Jersey"),
            ("S", "Infrastructure", "JE0000000011", "MAIN MARKET - SFS", "SHRS", "", "ORD", "Jersey"),
            ("T", "Property", "GB0000000012", "MAIN MARKET", "SHRS", "", "ORD", "United Kingdom"),
            ("X", "Test ETF", "GB0000000013", "MAIN MARKET", "ETFS", "", "", "United Kingdom"),
            ("Y", "Trade Only", "GB0000000014", "ADMISSION TO TRADING ONLY", "SHRS", "", "", "United Kingdom"),
            ("Z", "Depositary", "GB0000000015", "AIM", "DPRS", "", "", ""),
        ]
        result = _lse_list(rows, "https://source.example", set(), {"GB0000000012"}, "2026-10-31")
        self.assertEqual([(x.ticker, x.market, x.kind) for x in result], [
            ("C", "AIM", "Company"), ("S", "Main Market", "Fund"),
            ("T", "Main Market", "REIT"), ("Z", "AIM", "Company")])

    def test_aquis_includes_suspended_admissions_and_filters_bonds(self):
        def row(symbol, isin, segment, security, status):
            return dict(symbol=symbol, isin=isin, name=symbol, listing_type_name=segment,
                        instrument_type=security, status=status,
                        recent_prices=[dict(datetime="2026-11-18")])
        payload = dict(props=dict(pageProps=dict(companies=[
            row("R", "GB0000000010", "Aram", "Ordinary Shares", "ACTIVE"),
            row("S", "GB0000000011", "Access", "Ordinary Shares", "SUSPENDED"),
            row("B", "GB0000000012", "Apex", "Bonds", "ACTIVE"),
            row("M", "GB0000000013", "Trading", "Ordinary Shares", "ACTIVE"),
        ])))
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"
        result = _parse_aquis(html, date(2026, 11, 19))
        self.assertEqual([(x.ticker, x.status, x.fmp_symbol) for x in result],
                         [("R", "Active", ""), ("S", "Suspended", "")])
        with self.assertRaisesRegex(ValueError, "not current"):
            _parse_aquis(html, date(2026, 12, 20))
        with self.assertRaisesRegex(ValueError, "not current"):
            _check_date("LSE", date(2026, 10, 31), date(2027, 1, 10), 55)

    def test_duplicates_across_venues_and_share_classes(self):
        rows = [
            Listing("ARBB", "GB0007922338", "Arbuthnot Banking Group PLC", "Aquis",
                    "Company", "https://aquis", "", instrument="Ordinary Shares"),
            Listing("ARBN", "GB00BJRHYM66", "Arbuthnot Banking Group PLC Non-Voting Shares",
                    "Aquis", "Company", "https://aquis", "", instrument="B Ordinary Shares"),
            Listing("ARBB", "GB0007922338", "ARBUTHNOT BANKING GROUP PLC", "AIM", "Company",
                    "https://lse", "ARBB.L", instrument="ORD 1P"),
        ]
        self.assertEqual(len(_deduplicate(rows)), 1)
        result = _deduplicate(rows)[0]
        self.assertEqual((result.market, result.ticker, result.venues), ("AIM", "ARBB", ("AIM", "Aquis")))

    def test_hq_does_not_use_incorporation_or_customer_location(self):
        listing = Listing("P", "GG00BLGZJW08", "Test", "AIM", "Company", "https://lse", "P.L", "Jersey")
        self.assertEqual(classify_hq(listing, None).status, "review")
        self.assertEqual(classify_hq(listing, {"country": "GB"}).status, "review")
        profile = {"country": "GB", "city": "London", "address": "100 Example St", "zip": "EC2M 2AT",
                   "description": "It is headquartered in London, United Kingdom, and sells in Canada."}
        self.assertEqual((classify_hq(listing, profile).status, classify_hq(listing, profile).country),
                         ("in_scope", "GB"))
        profile["isin"] = "GB00BLGZJW08"
        self.assertEqual(classify_hq(listing, profile).status, "review")

    def test_hq_excludes_confirmed_isle_of_man_but_includes_channel_islands(self):
        listing = Listing("P", "GG00BLGZJW08", "Test", "AIM", "Company", "https://lse", "P.L")
        profile = {"country": "IM", "city": "Douglas", "zip": "IM1 1AA", "address": "11 King St",
                   "description": "The company is headquartered in Douglas, Isle of Man."}
        self.assertEqual(classify_hq(listing, profile).status, "out_of_scope")
        profile = {"country": "JE", "city": "St Helier", "zip": "JE1 1AA", "address": "1 King St"}
        result = classify_hq(listing, profile)
        self.assertEqual((result.status, result.country), ("in_scope", "JE"))
        self.assertIn("Provisional", result.evidence)
        profile["description"] = "It is headquartered in New Jersey, United States."
        self.assertEqual(classify_hq(listing, profile).status, "review")

    def test_verified_hq_override_applies_without_profile(self):
        listing = Listing("PEEL", "GG00BLGZJW08", "Test", "AIM", "Company", "https://lse", "PEEL.L")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "overrides.json"
            path.write_text(json.dumps({listing.isin: {
                "country": "GB", "source": "https://issuer.example/about", "note": "London head office",
            }}))
            decision = classify_hq(listing, None, load_hq_overrides(path))
            self.assertEqual((decision.status, decision.country), ("in_scope", "GB"))
            self.assertIn("https://issuer.example/about", decision.evidence)
            path.write_text(json.dumps({listing.isin: {"country": "GB", "source": "", "note": ""}}))
            with self.assertRaisesRegex(ValueError, "Invalid HQ override"):
                load_hq_overrides(path)


if __name__ == "__main__":
    unittest.main()
