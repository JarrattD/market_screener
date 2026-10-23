import copy
import json
import sys
import types
import unittest
from datetime import date
from unittest.mock import patch

from screenerV3 import book_margin_output, negative_ev_output


class MissingSheet(Exception):
    pass


class Tab:
    def __init__(self, title, ident):
        self.title, self.id, self.rows, self.note = title, ident, [], ''
    def update(self, range_name, values, value_input_option):
        offset = int(range_name[1:]) - 1
        self.rows[offset:offset + len(values)] = copy.deepcopy(values)
    def get_all_values(self): return copy.deepcopy(self.rows)
    def get_note(self, cell): return self.note
    def update_note(self, cell, note): self.note = note
    def freeze(self, rows): pass
    def resize(self, rows, cols): pass
    def clear(self): self.rows = []


class Sheet:
    def __init__(self, title): self.title, self.tabs = title, []
    def worksheets(self): return list(self.tabs)
    def worksheet(self, title):
        for tab in self.tabs:
            if tab.title == title: return tab
        raise MissingSheet(title)
    def add_worksheet(self, title, rows=100, cols=10, index=None):
        tab = Tab(title, len(self.tabs) + 1)
        self.tabs.insert(len(self.tabs) if index is None else index, tab)
        return tab


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.stub = patch.dict(sys.modules, {'gspread': types.SimpleNamespace(WorksheetNotFound=MissingSheet)})
        self.stub.start(); self.addCleanup(self.stub.stop)

    def publish(self, module, sheet, when, symbols=('A',), config=None):
        rows = [['Ticker', 'Company']] + [[s, s + ' company'] for s in symbols]
        audit = [['Ticker', 'Status']] + [[s, 'match'] for s in symbols]
        records = [['Ticker', 'Record', 'JSON', 'Retrieved']] + [[s, 'Profile', '{}', 'now'] for s in symbols]
        return module.publish(sheet, when, rows, audit, {'status': 'complete', 'matched_rows': len(symbols)}, config or {'rule': 1}, records)

    def modules(self): return (book_margin_output, negative_ev_output)

    def test_first_run_and_repeat_preserve_helpers_and_sources(self):
        for module in self.modules():
            with self.subTest(module=module.__name__):
                sheet = Sheet(module.TITLE)
                ranked = 'Book Margin Ranked' if module is book_margin_output else 'Negative EV Ranked'
                sheet.add_worksheet(ranked).rows = [['manual dashboard']]
                sheet.add_worksheet('Read me').rows = [['manual guidance']]
                receipt = self.publish(module, sheet, date(2026, 10, 24))
                self.assertEqual([t.title for t in sheet.tabs][:2], [ranked, '24-Oct-2026'])
                snapshot = copy.deepcopy([(t.title, t.rows, t.note) for t in sheet.tabs])
                self.assertEqual(self.publish(module, sheet, date(2026, 10, 24)), receipt)
                self.assertEqual(snapshot, [(t.title, t.rows, t.note) for t in sheet.tabs])
                self.assertEqual(module.resume_complete(sheet, date(2026, 10, 24), {'rule': 1}), receipt)

    def test_cooldown_suppresses_only_published_rows_not_assessments(self):
        for module in self.modules():
            sheet = Sheet(module.TITLE)
            self.publish(module, sheet, date(2025, 10, 25), ('A',))
            old = copy.deepcopy(sheet.worksheet('25-Oct-2025').rows)
            self.publish(module, sheet, date(2026, 10, 24), ('A', 'B'))
            self.assertEqual(sheet.worksheet('24-Oct-2026').rows[1:], [['B', 'B company']])
            coverage = dict(sheet.worksheet('Coverage 24-Oct-2026').rows)
            self.assertEqual((coverage['qualifying_rows_before_cooldown'], coverage['cooldown_suppressed_rows'], coverage['matched_rows']), ('2', '1', '1'))
            self.assertIn(['A', 'match'], sheet.worksheet('Latest assessment audit').rows)
            self.assertIn(['A', 'Profile', '{}', 'now'], sheet.worksheet('Inputs 24-Oct-2026').rows)
            self.assertEqual(old, sheet.worksheet('25-Oct-2025').rows)
            self.assertEqual(self.publish(module, sheet, date(2026, 10, 24), ('A', 'B'))['status'], 'complete')
            # Suppression is not a fresh appearance; exact prior anniversary is eligible.
            self.publish(module, sheet, date(2026, 10, 25), ('A', 'B'))
            self.assertEqual(sheet.worksheet('25-Oct-2026').rows[1:], [['A', 'A company']])

    def test_leap_anniversary_and_historical_criteria(self):
        for module in self.modules():
            sheet = Sheet(module.TITLE)
            self.publish(module, sheet, date(2024, 2, 29), config={'z_old_rule': 1, 'a_old_rule': 2})
            self.publish(module, sheet, date(2025, 2, 27), config={'z_new_rule': 2, 'a_new_rule': 3})
            self.assertEqual(len(sheet.worksheet('27-Feb-2025').rows), 1)
            self.publish(module, sheet, date(2025, 2, 28), config={'z_new_rule': 2, 'a_new_rule': 3})
            self.assertEqual(len(sheet.worksheet('28-Feb-2025').rows), 2)

    def test_helpers_unfinished_and_manual_tabs_are_not_appearances(self):
        for module in self.modules():
            sheet = Sheet(module.TITLE)
            for title in ('Read me', 'Configuration', 'Book Margin Metrics', 'Negative EV Metrics', 'Coverage 1-Oct-2026', 'Inputs 1-Oct-2026', '1-Oct-2026', '2-Oct-2026'):
                tab = sheet.add_worksheet(title); tab.rows = [['Ticker'], ['A']]
                if title == '2-Oct-2026': tab.note = module.STAGING + 'other-run'
            self.publish(module, sheet, date(2026, 10, 24))
            self.assertEqual(len(sheet.worksheet('24-Oct-2026').rows), 2)

    def test_invalid_future_and_tampered_history_fail_before_writes(self):
        for module in self.modules():
            for kind in ('bad_date', 'future_date', 'receipt', 'rows', 'coverage', 'inputs', 'seal', 'status', 'tab_id'):
                with self.subTest(module=module.__name__, kind=kind):
                    sheet = Sheet(module.TITLE)
                    self.publish(module, sheet, date(2026, 10, 17))
                    tab = sheet.worksheet('17-Oct-2026')
                    if kind == 'bad_date': tab.title = '31-Feb-2026'
                    elif kind == 'future_date': tab.title = '25-Oct-2026'
                    elif kind == 'receipt': tab.note = module.PREFIX + '{'
                    elif kind == 'rows': tab.rows[1][0] = 'tamper'
                    elif kind == 'coverage': sheet.worksheet('Coverage 17-Oct-2026').rows[1][1] = 'tamper'
                    elif kind == 'inputs': sheet.worksheet('Inputs 17-Oct-2026').rows[1][0] = 'tamper'
                    elif kind == 'seal': sheet.worksheet('Inputs 17-Oct-2026').note = ''
                    else:
                        receipt = json.loads(tab.note[len(module.PREFIX):])
                        receipt[kind] = 'wrong'
                        tab.note = module.PREFIX + json.dumps(receipt)
                    snapshot = copy.deepcopy([(t.title, t.rows, t.note) for t in sheet.tabs])
                    with self.assertRaises(ValueError): self.publish(module, sheet, date(2026, 10, 24))
                    self.assertEqual(snapshot, [(t.title, t.rows, t.note) for t in sheet.tabs])

    def test_same_day_invalid_receipts_and_wrong_config_fail_closed(self):
        for module in self.modules():
            for field in ('status', 'tab_id', 'criteria_hash'):
                sheet = Sheet(module.TITLE)
                self.publish(module, sheet, date(2026, 10, 24), config={'z': 1, 'a': 2})
                tab = sheet.worksheet('24-Oct-2026')
                receipt = json.loads(tab.note[len(module.PREFIX):])
                receipt[field] = 'wrong'
                tab.note = module.PREFIX + json.dumps(receipt)
                with self.assertRaises(ValueError):
                    module.resume_complete(sheet, date(2026, 10, 24), {'z': 1, 'a': 2})
            sheet = Sheet(module.TITLE)
            self.publish(module, sheet, date(2026, 10, 24), config={'z': 1, 'a': 2})
            with self.assertRaises(ValueError):
                module.resume_complete(sheet, date(2026, 10, 24), {'z': 3, 'a': 2})

    def test_duplicate_date_alias_fails_closed(self):
        for module in self.modules():
            sheet = Sheet(module.TITLE)
            self.publish(module, sheet, date(2026, 10, 1))
            original = sheet.worksheet('1-Oct-2026')
            alias = sheet.add_worksheet('01-Oct-2026')
            alias.rows, alias.note = copy.deepcopy(original.rows), original.note
            with self.assertRaises(ValueError):
                self.publish(module, sheet, date(2026, 10, 24))

    def test_independent_workbooks_and_normalised_symbols(self):
        first, second = Sheet(book_margin_output.TITLE), Sheet(negative_ev_output.TITLE)
        self.publish(book_margin_output, first, date(2026, 10, 17), ('a',))
        self.publish(book_margin_output, first, date(2026, 10, 24), (' A ',))
        self.publish(negative_ev_output, second, date(2026, 10, 24), ('A',))
        self.assertEqual(len(first.worksheet('24-Oct-2026').rows), 1)
        self.assertEqual(len(second.worksheet('24-Oct-2026').rows), 2)


if __name__ == '__main__': unittest.main()
