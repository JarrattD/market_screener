import copy
import importlib
import sys
import types
import unittest
from datetime import date, timedelta
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

    def history(self, file, as_of=date(2026, 10, 24)):
        sheet = self.Sheet.__new__(self.Sheet)
        sheet.file = file
        with patch('screenerV3.completion.london_batch_date', return_value=as_of):
            return sheet.get_all_previously_seen_tickers()

    def test_calendar_window_is_independent_of_tab_order_and_count(self):
        raw = [Tab((date(2026, 10, 23) - timedelta(days=i)).strftime('%d-%b-%Y')) for i in range(60)]
        old = Tab('23-Oct-2025')
        ranked = Tab('V2 Ranked')
        companions = [ranked, Tab('V2 Run Map'), Tab('V2 Metrics'), Tab('V2 Refresh Log')]
        expected = [w.title + '-ticker' for w in raw]
        self.assertEqual(self.history(File(raw + [old] + companions)), expected)
        self.assertEqual(self.history(File([ranked, old] + raw + companions[1:])), expected)
        self.assertEqual(len(expected), 60)

    def test_anniversary_is_eligible_and_same_day_still_on_cooldown(self):
        tabs = [Tab('23-Oct-2025'), Tab('24-Oct-2025'), Tab('25-Oct-2025'), Tab('24-Oct-2026')]
        self.assertEqual(self.history(File(tabs)), [w.title + '-ticker' for w in tabs[2:]])

    def test_leap_day_anniversary_clamps_to_last_day_of_february(self):
        tabs = [Tab('27-Feb-2023'), Tab('28-Feb-2023'), Tab('1-Mar-2023')]
        self.assertEqual(self.history(File(tabs), date(2024, 2, 29)), ['1-Mar-2023-ticker'])
        leap = [Tab('28-Feb-2024'), Tab('29-Feb-2024'), Tab('1-Mar-2024')]
        self.assertEqual(self.history(File(leap), date(2025, 3, 1)), [])
        self.assertEqual(self.history(File(leap), date(2025, 2, 28)), ['1-Mar-2024-ticker'])
        self.assertEqual(self.history(File([Tab('29-Feb-2024')]), date(2025, 2, 27)), ['29-Feb-2024-ticker'])

    def test_latest_actual_appearance_controls_without_refresh_dates(self):
        old, recent = Tab('24-Oct-2025'), Tab('1-Mar-2026')
        old.get_values = recent.get_values = lambda _: [['ABC'], ['abc'], [' ABC ']]
        file = File([old, recent, Tab('V2 Metrics')])
        self.assertEqual(self.history(file), ['ABC', 'abc', ' ABC '])
        self.assertEqual(self.history(file, date(2027, 3, 1)), [])

    def test_invalid_and_future_dates_fail_closed(self):
        for title in ['unknown', 'V2 Ranked archive', '31-Feb-2026', '1-Foo-2026', '25-Oct-2026']:
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.history(File([Tab(title)]))

    def test_other_workbooks_unchanged(self):
        tabs = [Tab('V2 Ranked')] + [Tab(str(i)) for i in range(55)]
        file = File(tabs, 'Other Screener')
        self.assertEqual(history_worksheets(file, tabs), tabs[:52])
        self.assertEqual(self.history(file), [w.title + '-ticker' for w in tabs[:52]])
        self.assertEqual(output_tab_options(file, 'beta'), {})

    def test_empty_or_only_companion_tabs(self):
        self.assertEqual(self.history(File([])), [])
        self.assertEqual(self.history(File([Tab('V2 Ranked'), Tab('V2 Metrics')])), [])

    def test_main_window_and_all_companions_are_order_independent(self):
        raw = [Tab((date(2026, 10, 23) - timedelta(days=i)).strftime('%d-%b-%Y')) for i in range(60)]
        companions = [Tab(name) for name in ['Main Ranked', 'Main Run Map', 'Main Metrics', 'Main Refresh Log']]
        expected = [w.title + '-ticker' for w in raw]
        self.assertEqual(self.history(File(raw + [Tab('24-Oct-2025')] + companions, 'Screener')), expected)
        self.assertEqual(self.history(File(companions + raw + [Tab('24-Oct-2025')], 'Screener')), expected)

    def test_main_anniversary_leap_and_unknown_dates(self):
        tabs = [Tab('28-Feb-2024'), Tab('29-Feb-2024'), Tab('1-Mar-2024')]
        self.assertEqual(self.history(File(tabs, 'Screener'), date(2025, 2, 28)), ['1-Mar-2024-ticker'])
        for title in ['V2 Ranked', 'Main Ranked archive', '31-Feb-2026', '25-Oct-2026']:
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.history(File([Tab(title)], 'Screener'))

    def test_main_insertion_requires_correct_kind_and_ranked_first(self):
        self.assertEqual(output_tab_options(File([Tab('Main Ranked')], 'Screener'), 'alpha'), {'index': 1})
        self.assertEqual(output_tab_options(File([Tab('Main Ranked')], 'Screener'), 'beta'), {})
        self.assertEqual(output_tab_options(File([Tab('old'), Tab('Main Ranked')], 'Screener'), 'alpha'), {})
        self.assertEqual(output_tab_options(File([Tab('Main Ranked')], 'Other Screener'), 'alpha'), {})

    def test_main_verified_output_preserves_values_and_is_idempotent(self):
        file = File([Tab('Main Ranked'), Tab('17-Oct-2026'), Tab('Main Metrics')], 'Screener')
        originals = copy.deepcopy([tab.values for tab in file.tabs])
        sheet = types.SimpleNamespace(file=file)
        data = {'ABC': {'Name': 'Example', 'NCAV Ratio': .5, 'EV/aFCF': 3,
                       'Payback Rating': 2, '5Y average': .2, 'HQ Location': 'GB',
                       'FV Upside Metric': 50, '5Y Price Metric': 25}}
        receipt = write_verified(sheet, 'alpha', data, date(2026, 10, 24))
        self.assertEqual([w.title for w in file.tabs], ['Main Ranked', '24-Oct-2026', '17-Oct-2026', 'Main Metrics'])
        self.assertEqual([file.tabs[i].values for i in [0, 2, 3]], originals)
        self.assertEqual(write_verified(sheet, 'alpha', data, date(2026, 10, 24)), receipt)
        self.assertEqual(len(file.calls), 1)

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

