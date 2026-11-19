import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from screenerV3.negative_ev_provider import FMPError
from screenerV3.uk_ci_fmp_universe import (
    LSE_UNSPLIT, fetch_fmp_universe, _screen, _venue,
)


def row(symbol, name, venue="LSE", **extra):
    return {"symbol": symbol, "companyName": name, "exchange": venue,
            "isActivelyTrading": True, "isEtf": False, **extra}


class FakeClient:
    concurrency = 2
    use_profile_bulk = True
    _fatal = None

    def __init__(self, markets, profiles=None):
        self.markets = markets
        self.profiles = profiles or {}
        self.calls = []

    async def _request(self, route, params, **kwargs):
        self.calls.append((route, params))
        if route.endswith("company-screener"):
            by_page = self.markets.get(params["exchange"], {})
            if isinstance(by_page, Exception):
                raise by_page
            return by_page.get(params["page"], [])
        if route.endswith("profile-bulk"):
            if params["part"] == 0:
                return list(self.profiles.values())
            return []
        return [self.profiles[params["symbol"]]] if params["symbol"] in self.profiles else []


class FMPUniverseTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_venue_identity_not_assumed_and_multi_market_dedup(self):
        samples = {
            "LSE": {0: [row("FUND.L", "An Investment Trust", isFund=True),
                        row("REIT.L", "Example Real Estate", industry="REIT - Retail"),
                        row("OVR.L", "Overseas plc", country="JP"),
                        row("OVERSEAS.NY", "Not a UK listing", "NYSE"),
                        row("ETF.L", "Bad ETF", "LSE", securityType="ETF")]},
            "AIM": {0: [row("PN.L", "A Test Co", "AIM")]},
            "AQSE": {0: [row("PN.AQ", "A Test Co", "AQSE")]},
        }
        profiles = {
            "FUND.L": row("FUND.L", "An Investment Trust", isin="GB0000000001", address="Here"),
            "REIT.L": row("REIT.L", "Example Real Estate", isin="GG0000000002", address="There"),
            "OVR.L": row("OVR.L", "Overseas plc", isin="JP0000000003", country="JP"),
            "PN.L": row("PN.L", "A Test Co", "AIM", isin="GB0000000004"),
            "PN.AQ": row("PN.AQ", "A Test Co", "AQSE", isin="GB0000000004"),
        }
        c = FakeClient(samples, profiles)
        result, mapped = await fetch_fmp_universe(c, now=datetime(2026, 11, 19, tzinfo=timezone.utc))
        by_symbol = {item.fmp_symbol: item for item in result}
        self.assertEqual(len(result), 4)
        self.assertEqual(len(mapped), 4)
        self.assertEqual(by_symbol["FUND.L"].kind, "Fund")
        self.assertEqual(by_symbol["REIT.L"].kind, "REIT")
        self.assertEqual(by_symbol["OVR.L"].market, LSE_UNSPLIT)
        self.assertEqual(by_symbol["PN.L"].venues, ("AIM", "Aquis"))
        self.assertEqual(c.uk_universe_stats["duplicate_issuer_symbols_collapsed"], 1)
        self.assertFalse(any(call[0] == "/stable/profile" for call in c.calls))
        for route, params in c.calls:
            if route.endswith("company-screener"):
                self.assertNotIn("country", params)
                self.assertNotIn("isFund", params)
                self.assertNotIn("marketCapMoreThan", params)
                self.assertEqual(params["includeAllShareClasses"], "true")

    async def test_missing_profile_retained_and_has_separate_issuer_key(self):
        c = FakeClient({"LSE": {0: [row("M.L", "Missing Identity"), row("X.L", "Known Identity")]}},
                       {"X.L": row("X.L", "Known Identity", isin="GB0000000002", country="GB")})
        listings, profiles = await fetch_fmp_universe(c)
        missing = next(x for x in listings if x.fmp_symbol == "M.L")
        self.assertEqual(missing.isin, "")
        self.assertEqual(missing.issuer_key, "FMP:M.L")
        self.assertNotIn(missing.issuer_key, profiles)

    async def test_more_specific_venue_only_if_explicit_and_conflict_is_review(self):
        known = row("K.L", "Known")
        samples = {"LSE": {0: [known, row("BAD.L", "Bad")]}}
        profiles = {
            "K.L": row("K.L", "Known", isin="GB0000000002", marketSegment="AIM"),
            "BAD.L": row("BAD.L", "Bad", "AQUIS", isin="GB0000000003"),
        }
        listings, matches = await fetch_fmp_universe(FakeClient(samples, profiles))
        by_symbol = {item.fmp_symbol: item for item in listings}
        self.assertEqual(by_symbol["K.L"].market, "AIM")
        self.assertEqual(by_symbol["BAD.L"].market, LSE_UNSPLIT)
        self.assertNotIn(by_symbol["BAD.L"].issuer_key, matches)
        self.assertEqual(_venue({"symbol": "ONLY.L"}), "")
        self.assertEqual(_venue({"mic": "XLOM"}), "")
        self.assertEqual(_venue({"mic": "AIMX"}), "AIM")
        self.assertEqual(_venue({"mic": "AQSG"}), "Aquis")
        self.assertEqual(_venue({"mic": "AQXE"}), "")
        self.assertEqual(_venue({"mic": "AQXA"}), "")

    async def test_pagination_and_duplicate_page_guard(self):
        with patch("screenerV3.uk_ci_fmp_universe.PAGE_SIZE", 2):
            p0 = [row("A.L", "A"), row("B.L", "B")]
            p1 = [row("C.L", "C")]
            rows = await _screen(FakeClient({"LSE": {0: p0, 1: p1}}), "LSE")
            self.assertEqual([r["symbol"] for r in rows], ["A.L", "B.L", "C.L"])
            with self.assertRaisesRegex(FMPError, "repeated_page"):
                await _screen(FakeClient({"LSE": {0: p0, 1: p0}}), "LSE")

    async def test_unavailable_optional_venue_does_not_discard_lse(self):
        c = FakeClient({"LSE": {0: [row("X.L", "Known Identity")]},
                        "AIM": FMPError("outside provider plan", 403),
                        "AQSE": FMPError("unknown alias", 400)},
                       {"X.L": row("X.L", "Known Identity", isin="GB0000000002")})
        listings, _ = await fetch_fmp_universe(c)
        self.assertEqual([item.fmp_symbol for item in listings], ["X.L"])
        self.assertEqual(c.uk_universe_stats["AIM FMP query unavailable (HTTP 403)"], 1)
        self.assertEqual(c.uk_universe_stats["AQSE FMP query unavailable (HTTP 400)"], 1)

    async def test_bulk_can_identify_genuinely_tagged_aquis_candidate(self):
        c = FakeClient({"LSE": {0: [row("X.L", "Known Identity")]}},
                       {"X.L": row("X.L", "Known Identity", isin="GB0000000002", country="GB"),
                        "Q.AQ": row("Q.AQ", "Aquis example", "AQUIS", isin="GG0000000003", country="GG")})
        listings, profiles = await fetch_fmp_universe(c)
        self.assertEqual(len(listings), 2)
        self.assertEqual({item.market for item in listings}, {LSE_UNSPLIT, "Aquis"})
        self.assertIn("GG0000000003", profiles)


if __name__ == "__main__":
    unittest.main()
