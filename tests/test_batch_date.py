"""Offline regression tests for the Friday UK batch and delayed launches."""
from datetime import date
import unittest
from urllib.parse import parse_qs, urlsplit
from screenerV3.batch_date import batch_window, run_batch_date
from tests.test_screener_ordering import gate, run, payload

class FridayBatchTests(unittest.TestCase):
    def scheduled(self, timestamp, **extra):
        return dict(event="schedule", created_at=timestamp, **extra)

    def test_winter_cross_midnight(self):
        for timestamp in ["2026-11-13T18:00:00Z", "2026-11-14T00:30:00Z",
                          "2026-11-15T17:59:59Z"]:
            self.assertEqual(run_batch_date(self.scheduled(timestamp)), date(2026,11,13))

    def test_summer_uses_british_time(self):
        self.assertEqual(run_batch_date(self.scheduled("2027-06-04T17:00:00Z")), date(2027,6,4))
        with self.assertRaises(ValueError):
            run_batch_date(self.scheduled("2027-06-04T16:59:59Z"))

    def test_outside_launch_window_fails_closed(self):
        for timestamp in ["2026-11-13T17:59:59Z", "2026-11-15T18:00:00Z",
                          "2026-11-16T00:01:00Z"]:
            with self.assertRaises(ValueError):
                run_batch_date(self.scheduled(timestamp))

    def test_rerun_uses_original_creation_not_update(self):
        self.assertEqual(run_batch_date(self.scheduled("2026-11-14T01:00:00Z",
            run_attempt=2, updated_at="2026-11-23T12:00:00Z")), date(2026,11,13))

    def test_pre_rollout_preserved(self):
        self.assertEqual(run_batch_date(self.scheduled("2026-11-07T03:00:00Z")),
                         date(2026,11,7))
        self.assertEqual(run_batch_date(self.scheduled("2026-10-24T23:30:00Z")),
                         date(2026,10,25))

    def test_manual_and_issues_remain_calendar_day(self):
        for event in ["workflow_dispatch","issues"]:
            self.assertEqual(run_batch_date(dict(event=event,created_at="2026-11-14T00:30:00Z")),
                             date(2026,11,14))

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            run_batch_date(self.scheduled("2026-11-14T00:30:00"))

    def test_gate_matches_friday_and_delayed_saturday(self):
        main=run(created_at="2026-11-13T18:05:00Z")
        child=run(102,child=True,created_at="2026-11-14T01:15:00Z")
        self.assertEqual(gate.select_main_run(payload(main),child),main)
        query=parse_qs(urlsplit(gate._list_path(child)).query)
        self.assertEqual(query["created"],["2026-11-13T18:00:00Z..2026-11-15T17:59:59Z"])

    def test_gate_still_rejects_ambiguous_or_wrong_commit(self):
        child=run(102,child=True,created_at="2026-11-14T01:15:00Z")
        main=run(created_at="2026-11-13T18:05:00Z")
        other=run(103,created_at="2026-11-14T02:00:00Z")
        with self.assertRaises(gate.GateError):
            gate.select_main_run(payload(main,other),child)
        main["head_sha"]="b"*40
        with self.assertRaises(gate.GateError):
            gate.select_main_run(payload(main),child)

if __name__ == "__main__":
    unittest.main()
