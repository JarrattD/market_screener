"""Shared offline quote acquisition and cross-market freshness regressions."""
import unittest
from datetime import datetime, timedelta, timezone, date
from unittest.mock import AsyncMock

from screenerV3.data_freshness import (
    DataFreshnessError, cutoff_months, quote_timestamp,
    require_quote_current, require_statement_current,
)
from screenerV3.quote_refresh import refresh_quotes
from screenerV3.negative_ev_provider import (
    normalize_profile, reconcile_cap_units, Issue, FMPError, FMPAuthenticationError,
)


def profile(symbol="ONE", **changes):
    return normalize_profile(dict(symbol=symbol, currency="USD", marketCap=9999999999,
        price=999, isActivelyTrading=True, isEtf=False, isFund=False, **changes))


class Client:
    concurrency = 3
    _fatal = None
    _legacy_available = True
    def __init__(self, request):
        self._request = AsyncMock(side_effect=request)
        self.metrics, self.health = {}, {}


class QuoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def row(self, symbol="ONE", **changes):
        return dict(dict(symbol=symbol, marketCap=50, price=1,
                         timestamp=int(self.now.timestamp())), **changes)

    async def run_refresh(self, request, profiles=None, **options):
        client = Client(request)
        profiles = profiles if profiles is not None else {"ONE": profile()}
        result, issues = await refresh_quotes(client, profiles,
            normalize_profile=normalize_profile, reconcile_cap_units=reconcile_cap_units,
            issue_type=Issue, error_type=FMPError, auth_error_type=FMPAuthenticationError,
            now=self.now, **options)
        return result, issues, client

    async def test_fresh_quote_replaces_large_profile_cap_before_filter(self):
        original = {"ONE": profile()}
        result, issues, client = await self.run_refresh(lambda *a: [self.row()], original)
        self.assertEqual(result["ONE"]["cap_amount"], 50)
        self.assertEqual(original["ONE"]["cap_amount"], 9999999999)
        self.assertTrue(result["ONE"]["quote_verified"])
        self.assertEqual(result["ONE"]["as_of"], self.now.isoformat())
        self.assertEqual(result["ONE"]["quote_evidence"]["timestamp"], int(self.now.timestamp()))
        self.assertFalse(issues)

    async def test_every_eligible_symbol_gets_quote_without_size_country_filter(self):
        data = {s: profile(s) for s in ("TINY", "HUGE", "0700.HK", "7203.T")}
        result, issues, client = await self.run_refresh(
            lambda path, params: [self.row(s) for s in params["symbols"].split(",")], data)
        self.assertEqual(client.metrics["quotes_verified"], 4)
        self.assertEqual(set(result), set(data))

    async def test_inactive_and_unknown_eligibility_are_not_fetched(self):
        data = {"ONE": profile(), "OFF": dict(profile("OFF"), security_eligible=False),
                "UNKNOWN": dict(profile("UNKNOWN"), security_eligible=None)}
        _, _, client = await self.run_refresh(lambda *a: [self.row()], data)
        self.assertEqual(client._request.call_args.args[1]["symbols"], "ONE")

    async def test_partial_success_recovers_only_missing_symbol(self):
        def request(path, params):
            return [self.row("ONE")] if path.endswith("batch-quote") else [self.row(params["symbol"])]
        result, issues, client = await self.run_refresh(request, {s: profile(s) for s in ("ONE", "TWO")})
        self.assertFalse(issues)
        self.assertEqual(client._request.call_args_list[1].args, ("/stable/quote", {"symbol": "TWO"}))

    async def test_stale_missing_future_and_invalid_timestamps_clear_old_cap(self):
        for stamp in (None, "", True, "not-a-date", int((self.now-timedelta(days=8)).timestamp()),
                      int((self.now+timedelta(hours=2)).timestamp())):
            with self.subTest(stamp=stamp):
                result, issues, client = await self.run_refresh(lambda *a: [self.row(timestamp=stamp)])
                self.assertIsNone(result["ONE"]["cap_amount"])
                self.assertIsNone(result["ONE"]["marketCap"])
                self.assertIsNone(result["ONE"]["price"])
                self.assertEqual(result["ONE"]["as_of"], "")
                self.assertTrue(client.health["systemic_failure"])
                self.assertTrue(issues)
                self.assertEqual(client._request.call_count, 1)

    async def test_retrieval_time_never_substitutes_for_quote_date(self):
        result, _, _ = await self.run_refresh(lambda *a: [self.row(timestamp=None)],
            {"ONE": dict(profile(), fetched_at=self.now.isoformat(), as_of=self.now.isoformat())})
        self.assertEqual(result["ONE"]["as_of"], "")

    async def test_invalid_cap_or_price_never_falls_back_to_profile(self):
        for field in ("marketCap", "price"):
            for value in (None, 0, -1, float("nan"), float("inf"), True):
                result, issues, _ = await self.run_refresh(lambda *a: [self.row(**{field: value})])
                self.assertIsNone(result["ONE"]["cap_amount"])
                self.assertTrue(issues)

    async def test_conflicting_same_timestamp_does_not_pick_arbitrary_row(self):
        result, issues, _ = await self.run_refresh(lambda *a: [self.row(), self.row(marketCap=100)])
        self.assertIsNone(result["ONE"]["cap_amount"])
        self.assertEqual(issues[0].reason, "conflicting_latest_quotes")

    async def test_latest_quote_chosen_not_provider_order(self):
        older = self.row(marketCap=999, timestamp=int((self.now-timedelta(days=1)).timestamp()))
        for rows in ([older, self.row()], [self.row(), older]):
            result, _, _ = await self.run_refresh(lambda *a: rows)
            self.assertEqual(result["ONE"]["cap_amount"], 50)

    async def test_currency_disagreement_is_unresolved(self):
        result, issues, _ = await self.run_refresh(lambda *a: [self.row(currency="JPY")])
        self.assertIsNone(result["ONE"]["cap_amount"])
        self.assertEqual(issues[0].reason, "quote_currency_conflicts_with_profile")

    async def test_access_denial_does_not_try_symbol_or_legacy_route(self):
        def denied(*a): raise FMPError("endpoint_access_denied", 403)
        result, issues, client = await self.run_refresh(denied,
            {str(i): profile(str(i)) for i in range(105)})
        self.assertEqual(client._request.call_count, 1)
        self.assertEqual(len(issues), 105)
        self.assertTrue(all(p["cap_amount"] is None for p in result.values()))

    async def test_authentication_failure_propagates(self):
        with self.assertRaises(FMPAuthenticationError):
            await self.run_refresh(lambda *a: (_ for _ in ()).throw(FMPAuthenticationError("auth",401)))

    async def test_fatal_quota_failure_propagates(self):
        client = Client(None)
        error = FMPError("rate_limit_retries_exhausted",429)
        async def request(*a):
            client._fatal = error
            raise error
        client._request = request
        with self.assertRaises(FMPError):
            await refresh_quotes(client,{"ONE":profile()}, normalize_profile=normalize_profile,
                reconcile_cap_units=reconcile_cap_units,issue_type=Issue,error_type=FMPError,
                auth_error_type=FMPAuthenticationError)

    async def test_missing_batch_url_uses_supported_symbol_route(self):
        def request(path, params):
            if path.endswith("batch-quote"): raise FMPError("endpoint_unavailable",404)
            return [self.row(params["symbol"])]
        result, issues, client = await self.run_refresh(request)
        self.assertEqual(result["ONE"]["cap_amount"], 50)
        self.assertFalse(issues)

    async def test_batch_size_is_bounded_and_no_symbols_are_truncated(self):
        data = {str(i): profile(str(i)) for i in range(205)}
        result, issues, client = await self.run_refresh(
            lambda p,args: [self.row(s) for s in args["symbols"].split(",")], data)
        self.assertEqual(client._request.call_count,3)
        self.assertEqual(client.metrics["quotes_verified"],205)

    async def test_subunit_uses_coherent_new_quote_not_blind_division(self):
        data = {"LLOY.L": normalize_profile(dict(symbol="LLOY.L", currency="GBp",
                marketCap=1, price=1, isActivelyTrading=True,isEtf=False,isFund=False))}
        current = self.row("LLOY.L",price=103.25,marketCap=59_198_770_651,sharesOutstanding=57_335_371_091)
        def request(path, params=None):
            return [self.row("LLOY.L",marketCap=58_854_758_425,price=102.65)] if path.endswith("batch-quote") else [current]
        result, issues, client = await self.run_refresh(request,data)
        self.assertEqual(result["LLOY.L"]["cap_amount"],59_198_770_651)
        self.assertEqual(result["LLOY.L"]["cap_currency"],"GBP")
        self.assertEqual(result["LLOY.L"]["quote_source"],"/api/v3/quote")
        self.assertFalse(issues)

    async def test_subunit_missing_current_shares_is_unresolved(self):
        data = {"ONE": normalize_profile(dict(symbol="ONE",currency="GBp",marketCap=50,
                isActivelyTrading=True,isEtf=False,isFund=False))}
        result, issues, _ = await self.run_refresh(lambda *a: [self.row()],data)
        self.assertIsNone(result["ONE"]["cap_amount"])
        self.assertTrue(issues)

    async def test_batch_failure_preserves_other_success_and_cannot_reuse_old_caps(self):
        data = {s: profile(s) for s in ("ONE", "TWO")}
        def request(path, params):
            if params["symbols"] == "ONE": return [self.row()]
            raise FMPError("provider_unavailable_retries_exhausted",503)
        result, issues, client = await self.run_refresh(request,data,batch_size=1)
        self.assertEqual(result["ONE"]["cap_amount"],50)
        self.assertIsNone(result["TWO"]["cap_amount"])
        self.assertIsNone(result["TWO"]["marketCap"])
        self.assertNotIn("systemic_failure",client.health)
        self.assertEqual(len(issues),1)


class FreshnessTests(unittest.TestCase):
    def test_calendar_cutoff_clamps_short_months(self):
        self.assertEqual(cutoff_months(date(2026,8,31),18),date(2025,2,28))
        self.assertEqual(cutoff_months(date(2025,8,31),18),date(2024,2,29))

    def test_statement_age_boundary_and_cross_market_recent_dates(self):
        today=date(2026,10,19)
        for day in ("2025-04-19","2025-12-31","2026-03-31","2026-06-30"):
            require_statement_current({"date":day},"Annual",today)
        for day in ("2025-04-18","2019-12-31","2004-12-31","",None,"2026-10-20"):
            with self.assertRaises(DataFreshnessError):
                require_statement_current({"date":day},"Annual",today)

    def test_quote_age_boundary_and_weekend_allowance(self):
        now=datetime(2026,10,19,tzinfo=timezone.utc)
        require_quote_current({"as_of":(now-timedelta(days=7)).isoformat()},now)
        require_quote_current({"as_of":(now-timedelta(days=3)).isoformat()},now)
        with self.assertRaises(DataFreshnessError):
            require_quote_current({"as_of":(now-timedelta(days=7,seconds=1)).isoformat()},now)

    def test_timestamp_parser_never_uses_invalid_nonfinite_or_boolean(self):
        for value in (None,False,True,"bad",float("nan"),float("inf"),-1):
            self.assertIsNone(quote_timestamp(value))


if __name__ == "__main__":
    unittest.main()
