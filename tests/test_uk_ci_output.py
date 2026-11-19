from datetime import date
from types import SimpleNamespace
from unittest import TestCase

from screenerV3.uk_ci_output import COVERAGE, MARKER, RANKED, REVIEW, _matches, parse_ownership, sheet_requests


class OutputTests(TestCase):
    def test_native_numbers_raw_text_and_atomic_managed_range(self):
        tabs = {t: SimpleNamespace(id=i, row_count=100, col_count=25)
                for i, t in enumerate(("Ranked", "HQ Review", "Coverage"), 1)}
        row = [1, 7, "=not a formula", *([None] * (len(RANKED) - 3))]
        requests = sheet_requests(tabs, {"Ranked": [RANKED, row], "HQ Review": [REVIEW],
                                        "Coverage": [COVERAGE, ("Template", MARKER)]}, [{}])
        up = [r["updateCells"] for r in requests if "updateCells" in r]
        self.assertEqual(len(up), 3)
        self.assertEqual(up[0]["range"]["endRowIndex"], 100)
        ranked = up[0]["rows"][1]["values"]
        self.assertEqual(ranked[1]["userEnteredValue"], {"numberValue": 7})
        self.assertEqual(ranked[1]["userEnteredFormat"]["numberFormat"]["pattern"], '0"/7"')
        self.assertEqual(ranked[2]["userEnteredValue"], {"stringValue": "=not a formula"})

    def test_readback_compares_numeric_type_and_missing(self):
        self.assertTrue(_matches([["name", 0, 1.125]], [["name", 0, 1.125, None]]))
        self.assertFalse(_matches([["name", "0", 1.125]], [["name", 0, 1.125, None]]))
        self.assertFalse(_matches([["name", 0]], [["name", 0, 1.125, None]]))

    def test_ownership_stale_and_conflicting_rows_are_not_points(self):
        base = ["GB00BP6MXD84", "SHEL", 11, "2026-11-01", "https://example.org/file", "Holder"]
        end = date(2026, 11, 19)
        self.assertEqual(parse_ownership([base], end)["GB00BP6MXD84"]["pct"], 11)
        self.assertEqual(parse_ownership([base, base[:2] + [19] + base[3:]], end), {})
        self.assertEqual(parse_ownership([base[:3] + ["2024-11-01"] + base[4:]], end), {})
