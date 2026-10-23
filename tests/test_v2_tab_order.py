import copy
import importlib
import sys
import types
import unittest
from datetime import date
from unittest.mock import patch

from screenerV3.completion import write_verified
from screenerV3.tab_order import history_worksheets, output_tab_options


class Tab:
    def __init__(self, title, ident=1):
        self.title, self.id = title, ident
        self.values, self.note = [[title]], ''
    def get_values(self, range_name):
        assert range_name == 'A2:A1000'
        return [[self.title + '-ticker']]
    def update(self, **kwargs): self.values = copy.deepcopy(kwargs['values'])
    def get_all_values(self, **kwargs): return self.values
    def get_note(self, cell): return self.note
    def update_note(self, cell, note): self.note = note


class File:
    id = 'mock-workbook'
    def __init__(self, tabs, title='V2 Screener'):
        self.tabs, self.title, self.calls = tabs, title, []
    def worksheets(self): return self.tabs
    def add_worksheet(self, **kwargs):
        self.calls.append(kwargs)
        tab = Tab(kwargs['title'], 999)
        self.tabs.insert(kwargs.get('index', len(self.tabs)), tab)
        return tab


class TabOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The tests use no credentials or network. Permit a minimal import stub
        # when the optional Google client is not installed in a test environment.
        try:
            import gspread
        except ModuleNotFoundError:
            with patch.dict(sys.modules, {'gspread': types.SimpleNamespace(Worksheet=Tab)}):
                cls.Sheet = importlib.import_module('screenerV3.sheet').Sheet
        else:
            cls.Sheet = importlib.import_module('screenerV3.sheet').Sheet

    def history(self, file):
        sheet = self.Sheet.__new__(self.Sheet)
        sheet.file = file
        return sheet.get_all_previously_seen_tickers()

    def test_exact_52_tab_history_before_and_after_ranked_move(self):
        raw = [Tab('run-' + str(i)) for i in range(54)]
        ranked = Tab('V2 Ranked')
        companions = [ranked, Tab('V2 Run Map'), Tab('V2 Metrics'), Tab('V2 Refresh Log')]
        original = raw + companions
        moved = [ranked] + raw + companions[1:]
        expected = [w.title + '-ticker' for w in original[:52]]
        self.assertEqual(self.history(File(original)), expected)
        self.assertEqual(self.history(File(moved)), expected)
        universe = expected + ['new-ticker', raw[52].title + '-ticker']
        self.assertEqual([t for t in universe if t not in self.history(File(moved))],
                         [t for t in universe if t not in expected])

    def test_companions_interspersed_do_not_use_history_slots(self):
        raw = [Tab('one'), Tab('two')]
        self.assertEqual(history_worksheets(File([]), [Tab('V2 Metrics'), raw[0], Tab('V2 Ranked'), raw[1]]), raw)

    def test_other_workbooks_unchanged(self):
        tabs = [Tab('V2 Ranked')] + [Tab(str(i)) for i in range(55)]
        file = File(tabs, 'Screener')
        self.assertIs(history_worksheets(file, tabs), tabs)
        self.assertEqual(self.history(file), [w.title + '-ticker' for w in tabs[:52]])
        self.assertEqual(output_tab_options(file, 'beta'), {})

    def test_only_exact_companion_names_excluded(self):
        tab = Tab('V2 Ranked archive')
        self.assertEqual(history_worksheets(File([]), [tab]), [tab])

    def test_unmoved_ranked_or_missing_ranked_keeps_default_insertion(self):
        for tabs in [[], [Tab('old')], [Tab('old'), Tab('V2 Ranked')]]:
            self.assertEqual(output_tab_options(File(tabs), 'beta'), {})

    def test_alpha_keeps_default_insertion(self):
        self.assertEqual(output_tab_options(File([Tab('V2 Ranked')]), 'alpha'), {})

    def test_verified_beta_write_inserts_next_to_ranked_and_rerun_is_idempotent(self):
        ranked, old = Tab('V2 Ranked'), Tab('old')
        file = File([ranked, old, Tab('V2 Metrics')])
        before = copy.deepcopy([w.values for w in file.tabs])
        sheet = types.SimpleNamespace(file=file)
        data = {'ABC': {'Name': 'Example', 'NCAV Ratio': .5, 'EV/aFCF': 3,
                       'P/TBV Ratio': .7, 'HQ Location': 'GB',
                       'FV Upside Metric': 50, '5Y Price Metric': 25}}
        receipt = write_verified(sheet, 'beta', data, date(2026, 10, 24))
        self.assertEqual([w.title for w in file.tabs], ['V2 Ranked', '24-Oct-2026', 'old', 'V2 Metrics'])
        self.assertEqual(file.calls[0]['index'], 1)
        self.assertEqual([file.tabs[i].values for i in [0, 2, 3]], before)
        self.assertEqual(write_verified(sheet, 'beta', data, date(2026, 10, 24)), receipt)
        self.assertEqual(len(file.calls), 1)


if __name__ == '__main__': unittest.main()
