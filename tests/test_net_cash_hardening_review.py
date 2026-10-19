from datetime import datetime, timezone
"""Independent offline regressions for revision, quota, and publication safety."""
import contextlib
import copy
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import sys
import types
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import net_cash_screen
from screenerV3 import net_cash_output as output
from screenerV3.net_cash_calculations import Criteria
from screenerV3.net_cash_data import (
    FMPClient, FMPError, FXRate, five_year_cashflows, latest_statement,
)


def annual(year=2025, **changes):
    row = dict(symbol="SYNTH", date=f"{year}-12-31", fiscalYear=year,
               period="FY", reportedCurrency="GBP", revenue=100,
               grossProfit=30, totalDebt=10, cashAndShortTermInvestments=20,
               totalStockholdersEquity=100, goodwillAndIntangibleAssets=20,
               freeCashFlow=10)
    row.update(changes)
    return row


async def verified_quote_fixture(client, profiles, **kwargs):
    return profiles, []


class RevisionHardeningReviewTests(unittest.TestCase):
    def test_mixed_separator_revision_order_is_chronological(self):
        earlier = annual(acceptedDate="2026-02-10T09:00:00", grossProfit=30)
        later = annual(acceptedDate="2026-02-10 15:00:00", grossProfit=35)
        for rows in itertools.permutations([earlier, later]):
            with self.subTest(order=[row["acceptedDate"] for row in rows]):
                self.assertEqual(latest_statement(list(rows), "SYNTH", True)["grossProfit"], 35)

    def test_revision_offsets_are_compared_as_instants(self):
        earlier = annual(acceptedDate="2026-02-10T09:00:00+02:00", totalDebt=10)
        later = annual(acceptedDate="2026-02-10T08:00:00Z", totalDebt=20)
        self.assertEqual(latest_statement([earlier, later], "SYNTH")["totalDebt"], 20)

    def test_all_screen_material_fields_conflict_at_same_revision(self):
        fields = ("revenue", "grossProfit", "totalDebt", "cashAndShortTermInvestments",
                  "totalStockholdersEquity", "goodwillAndIntangibleAssets", "freeCashFlow")
        for field in fields:
            first = annual(acceptedDate="2026-02-10T09:00:00Z")
            second = dict(first, **{field:first[field] + 1}, acceptedDate="2026-02-10 09:00:00+00:00")
            with self.subTest(field=field):
                self.assertIsNone(latest_statement([first, second], "SYNTH"))
                self.assertIsNone(latest_statement([second, first], "SYNTH"))

    def test_undated_conflicting_revision_is_not_silently_discarded(self):
        unknown = annual(grossProfit=29)
        dated = annual(acceptedDate="2026-02-10 09:00:00", grossProfit=31)
        for rows in ([unknown, dated], [dated, unknown], [unknown, dict(unknown, grossProfit=31)]):
            self.assertIsNone(latest_statement(rows, "SYNTH", True))

    def test_identical_undated_or_equivalent_numeric_revisions_remain_usable(self):
        one = annual()
        two = dict(one, revenue="100.0", grossProfit="30", freeCashFlow="10.00")
        self.assertIsNotNone(latest_statement([one, two], "SYNTH", True))

    def test_newest_ambiguous_period_does_not_fall_back_to_old_year(self):
        rows = [annual(2024), annual(acceptedDate="2026-02-10", freeCashFlow=10),
                annual(acceptedDate="2026-02-10", freeCashFlow=100)]
        self.assertIsNone(latest_statement(rows, "SYNTH", True))

    def test_cashflow_ambiguity_survives_duplicate_permutations(self):
        clean = [annual(year) for year in range(2020, 2025)]
        first = annual(acceptedDate="2026-02-10T09:00:00Z", freeCashFlow=10)
        conflict = annual(acceptedDate="2026-02-10 09:00:00+00:00", freeCashFlow=100)
        for revisions in itertools.permutations([first, conflict, dict(first)]):
            selected, reason = five_year_cashflows(clean + list(revisions), "SYNTH")
            self.assertNotEqual(reason, "complete_five_consecutive_financial_years")

    def test_equal_time_cashflow_period_start_conflict_is_unresolved(self):
        clean = [annual(year) for year in range(2020, 2025)]
        full = annual(acceptedDate="2026-02-10", startDate="2025-01-01")
        stub = annual(acceptedDate="2026-02-10", startDate="2025-07-01")
        for revisions in ([full, stub], [stub, full]):
            selected, reason = five_year_cashflows(clean + revisions, "SYNTH")
            self.assertNotEqual(reason, "complete_five_consecutive_financial_years")

    def test_equivalent_period_start_aliases_are_not_false_conflicts(self):
        clean = [annual(year) for year in range(2020, 2025)]
        first = annual(acceptedDate="2026-02-10", startDate="2025-01-01")
        second = annual(acceptedDate="2026-02-10", periodStartDate="2025-01-01")
        selected, reason = five_year_cashflows(clean + [first, second], "SYNTH")
        self.assertEqual(reason, "complete_five_consecutive_financial_years")

    def test_cashflow_revision_with_later_known_instant_is_usable(self):
        rows = [annual(year) for year in range(2020, 2025)]
        rows += [annual(acceptedDate="2026-02-10T09:00:00", freeCashFlow=10),
                 annual(acceptedDate="2026-02-10 15:00:00", freeCashFlow=20)]
        selected, reason = five_year_cashflows(rows, "SYNTH")
        self.assertEqual(reason, "complete_five_consecutive_financial_years")
        self.assertEqual(selected[0]["freeCashFlow"], 20)


def profile(symbol):
    return dict(symbol=symbol, companyName="Synthetic " + symbol, currency="GBP",
                marketCap=50, isActivelyTrading=True, isEtf=False, isFund=False)


def open_fatal(client):
    client._fatal = FMPError("rate_limit_retries_exhausted", 429)
    raise client._fatal


class FatalQuotaHardeningReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_universe_fatal_after_first_directory_success_propagates(self):
        client = FMPClient("dummy-offline-key")
        async def request(path, params=None, **kwargs):
            if path == "/stable/stock-list":
                return [dict(symbol="SYNTH", type="stock")]
            return open_fatal(client)
        client._request = request
        with self.assertRaises(FMPError):
            await client.fetch_universe()

    async def test_profile_fatal_after_partial_bulk_success_propagates(self):
        client = FMPClient("dummy-offline-key", concurrency=1)
        async def request(path, params=None, **kwargs):
            if client._fatal:
                raise client._fatal
            if path == "/stable/profile-bulk" and params["part"] == 0:
                return [profile("A_GOOD")]
            return open_fatal(client)
        client._request = request
        with self.assertRaises(FMPError):
            await client.fetch_profiles(["A_GOOD", "Z_QUOTA"])

    async def test_fx_fatal_after_known_rate_propagates(self):
        client = FMPClient("dummy-offline-key")
        async def request(path, params=None, **kwargs):
            if path == "/api/v3/quotes/forex":
                return [dict(symbol="USDGBP", price=0.8,
                             timestamp=int(datetime.now(timezone.utc).timestamp()))]
            return open_fatal(client)
        client._request = request
        with self.assertRaises(FMPError):
            await client.fetch_fx(["GBP", "USD", "ZZZ"])

    def financial_client(self):
        client = FMPClient("dummy-offline-key", concurrency=1)
        async def request(path, params=None, **kwargs):
            if client._fatal:
                raise client._fatal
            symbol = path.rsplit("/", 1)[-1]
            if symbol == "Z_QUOTA":
                return open_fatal(client)
            if "cash-flow" in path:
                return [annual(year, symbol=symbol) for year in range(2020, 2026)]
            return [annual(symbol=symbol)]
        client._request = request
        return client

    async def test_financial_fatal_after_one_complete_candidate_propagates(self):
        client = self.financial_client()
        with self.assertRaises(FMPError):
            await client.fetch_financials(["A_GOOD", "Z_QUOTA"], include_cashflow=True)

    async def test_partial_success_and_fatal_quota_never_publish_complete_screen(self):
        client = self.financial_client()
        client.universe_source = "offline-review-fixture"
        async def universe():
            return [dict(symbol=symbol) for symbol in ("A_GOOD", "Z_QUOTA")]
        async def profiles(universe):
            return {row["symbol"]:dict(profile(row["symbol"]), cap_amount=50,
                        as_of=datetime.now(timezone.utc).isoformat(),cap_currency="GBP", security_eligible=True,
                        security_identity="SYMBOL:" + row["symbol"]) for row in universe}, []
        async def fx(currencies):
            return {c:FXRate(c, 1, "2026-10-19", "2026-10-19", "synthetic") for c in currencies}, []
        client.fetch_universe, client.fetch_profiles, client.fetch_fx = universe, profiles, fx
        with patch.object(net_cash_screen, "refresh_quotes", verified_quote_fixture), patch.object(net_cash_screen, "publish") as publish:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(FMPError):
                    await net_cash_screen.screen(client, object(), Criteria(cheapness_any=True))
            publish.assert_not_called()


class MissingTab(Exception):
    pass


class FakeTab:
    def __init__(self, sheet, title):
        self.sheet, self.title = sheet, title
        self.rows, self.note = [], ""
        self.id = len(sheet.tabs) + 1
        self.fail_update = False
    def get_all_values(self):
        return copy.deepcopy(self.rows)
    def get_note(self, cell):
        return self.note
    def update_note(self, cell, note):
        self.sheet.mutations.append((self.title, "note"))
        self.note = note
    def resize(self, rows, cols):
        self.sheet.mutations.append((self.title, "resize"))
    def clear(self):
        self.sheet.mutations.append((self.title, "clear"))
        self.rows = []
    def update(self, range_name, values, value_input_option):
        if self.fail_update:
            raise RuntimeError("synthetic interrupted write")
        self.sheet.mutations.append((self.title, "values"))
        offset = int(range_name[1:]) - 1
        self.rows[offset:offset + len(values)] = copy.deepcopy(values)
    def freeze(self, rows):
        self.sheet.mutations.append((self.title, "freeze"))


class FakeSheet:
    def __init__(self):
        self.tabs, self.mutations = {}, []
    def worksheet(self, title):
        if title not in self.tabs:
            raise MissingTab(title)
        return self.tabs[title]
    def add_worksheet(self, title, rows, cols):
        self.mutations.append((title, "create"))
        tab = FakeTab(self, title)
        self.tabs[title] = tab
        return tab
    def snapshot(self):
        return {title:(copy.deepcopy(tab.rows), tab.note) for title, tab in self.tabs.items()}


class PublicationHardeningReviewTests(unittest.TestCase):
    def setUp(self):
        self.gspread = patch.dict(sys.modules, {"gspread":types.SimpleNamespace(WorksheetNotFound=MissingTab)})
        self.gspread.start()
        self.addCleanup(self.gspread.stop)
        self.sheet = FakeSheet()
        self.day = date(2026, 10, 19)
        self.rows = [["Ticker", "Company"], ["SYNTH", "Synthetic"]]
        self.audit = [["Ticker", "Status"], ["SYNTH", "match"]]
        self.coverage = {"status":"complete", "assessment_utc":"2026-10-19T00:00:00+00:00"}
        self.config = {"rule":"synthetic-review"}
        self.records = [["Ticker", "Record", "Data", "Retrieved UTC"],
                        ["SYNTH", "Profile", "{}", "2026-10-19"]]

    def publish(self, **changes):
        args = dict(rows=self.rows, audit=self.audit, coverage=self.coverage,
                    config=self.config, records=self.records)
        args.update(changes)
        return output.publish(self.sheet, self.day, **args)

    def test_conflicting_repeat_publish_cannot_mutate_any_companion(self):
        self.publish()
        before, mutation_count = self.sheet.snapshot(), len(self.sheet.mutations)
        with self.assertRaises(ValueError):
            self.publish(rows=[["Ticker", "Company"], ["OTHER", "Different"]],
                         audit=[["Ticker", "Status"], ["OTHER", "match"]],
                         coverage=dict(self.coverage, assessment_utc="changed"),
                         records=self.records + [["OTHER", "Profile", "{}", "changed"]])
        self.assertEqual(self.sheet.snapshot(), before)
        self.assertEqual(len(self.sheet.mutations), mutation_count)

    def test_unowned_dated_companion_is_preflighted_before_audit_write(self):
        foreign = self.sheet.add_worksheet("Inputs 19-Oct-2026", 100, 10)
        foreign.rows, foreign.note = [["Do not alter"]], "human-owned"
        audit = self.sheet.add_worksheet("Latest assessment audit", 100, 10)
        audit.rows = [["Previous audit"]]
        before, mutation_count = self.sheet.snapshot(), len(self.sheet.mutations)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.sheet.snapshot(), before)
        self.assertEqual(len(self.sheet.mutations), mutation_count)

    def test_completed_companions_are_sealed_and_cannot_be_rewritten(self):
        self.publish()
        for title in ("Inputs 19-Oct-2026", "Coverage 19-Oct-2026"):
            with self.subTest(title=title):
                tab = self.sheet.worksheet(title)
                self.assertFalse(tab.get_note("A1").startswith(output.STAGING))
                before, count = self.sheet.snapshot(), len(self.sheet.mutations)
                with self.assertRaises(ValueError):
                    output.write_matrix(self.sheet, title, [["replacement"]])
                self.assertEqual(self.sheet.snapshot(), before)
                self.assertEqual(len(self.sheet.mutations), count)

    def test_same_complete_publish_is_read_only(self):
        receipt = self.publish()
        before, count = self.sheet.snapshot(), len(self.sheet.mutations)
        self.assertEqual(self.publish(), receipt)
        self.assertEqual(self.sheet.snapshot(), before)
        self.assertEqual(len(self.sheet.mutations), count)

    def test_resume_rejects_removed_companion_seal(self):
        self.publish()
        self.sheet.tabs["Inputs 19-Oct-2026"].note = ""
        with self.assertRaises(ValueError):
            output.resume_complete(self.sheet, self.day, self.config)

    def test_interrupted_final_receipt_conflicting_sealed_companion_is_read_only(self):
        self.publish()
        self.sheet.tabs["19-Oct-2026"].note = output.STAGING + output.RUN_OWNER
        before, count = self.sheet.snapshot(), len(self.sheet.mutations)
        with self.assertRaises(ValueError):
            self.publish(audit=[["Changed audit"]],
                         records=self.records + [["OTHER", "Profile", "{}", "new"]])
        self.assertEqual(self.sheet.snapshot(), before)
        self.assertEqual(len(self.sheet.mutations), count)

    def test_interrupted_final_receipt_same_data_can_complete(self):
        self.publish()
        self.sheet.tabs["19-Oct-2026"].note = output.STAGING + output.RUN_OWNER
        receipt = self.publish()
        self.assertEqual(output.resume_complete(self.sheet, self.day, self.config), receipt)
        self.assertEqual(self.sheet.tabs["Inputs 19-Oct-2026"].rows, self.records)

    def test_baseline_v1_receipt_still_resumes_by_verified_hashes(self):
        receipt = self.publish()
        legacy = dict(receipt)
        legacy.pop("seal_version")
        self.sheet.tabs["19-Oct-2026"].note = output.PREFIX + json.dumps(legacy, sort_keys=True)
        self.sheet.tabs["Coverage 19-Oct-2026"].note = output.STAGING + "legacy-run"
        self.sheet.tabs["Inputs 19-Oct-2026"].note = output.STAGING + "legacy-run"
        before, count = self.sheet.snapshot(), len(self.sheet.mutations)
        self.assertEqual(output.resume_complete(self.sheet, self.day, self.config), legacy)
        self.assertEqual(self.publish(), legacy)
        self.assertEqual(self.sheet.snapshot(), before)
        self.assertEqual(len(self.sheet.mutations), count)

    def test_baseline_v1_receipt_still_rejects_changed_inputs(self):
        receipt = self.publish()
        legacy = dict(receipt)
        legacy.pop("seal_version")
        self.sheet.tabs["19-Oct-2026"].note = output.PREFIX + json.dumps(legacy, sort_keys=True)
        self.sheet.tabs["Inputs 19-Oct-2026"].rows[-1][2] = "tampered"
        with self.assertRaises(ValueError):
            output.resume_complete(self.sheet, self.day, self.config)

    def load_fresh_output(self, label):
        spec = importlib.util.spec_from_file_location(label, Path(output.__file__))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_independent_local_invocations_have_distinct_staging_owners(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID":""}):
            first = self.load_fresh_output("_review_output_process_one")
            second = self.load_fresh_output("_review_output_process_two")
            first.write_matrix(self.sheet, "First local", [["Ticker"]])
            second.write_matrix(self.sheet, "Second local", [["Ticker"]])
        self.assertNotEqual(self.sheet.tabs["First local"].note, self.sheet.tabs["Second local"].note)

    def test_foreign_local_staging_is_not_adopted_or_overwritten(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID":""}):
            first = self.load_fresh_output("_review_output_owner_one")
            second = self.load_fresh_output("_review_output_owner_two")
            first.write_matrix(self.sheet, "Shared date", [["Ticker"], ["first"]])
            before, count = self.sheet.snapshot(), len(self.sheet.mutations)
            with self.assertRaises(ValueError):
                second.write_matrix(self.sheet, "Shared date", [["Ticker"], ["second"]])
        self.assertEqual(self.sheet.snapshot(), before)
        self.assertEqual(len(self.sheet.mutations), count)


if __name__ == "__main__":
    unittest.main()
