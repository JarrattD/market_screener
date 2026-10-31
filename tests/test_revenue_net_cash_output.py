"""No-network receipt, immutable companion and calendar cooldown regressions."""
import copy
from datetime import date
import json
import sys
import types
import unittest
from unittest.mock import patch
from screenerV3 import revenue_net_cash_output as out
from screenerV3.revenue_net_cash_cooldown import appearance_eligible_on

class MissingSheet(Exception): pass

class Tab:
    def __init__(self, title, ident):
        self.title, self.id, self.rows, self.note = title, ident, [], ''
    def update(self, range_name, values, value_input_option):
        offset = int(range_name[1:])-1
        self.rows[offset:offset+len(values)] = copy.deepcopy(values)
    def get_all_values(self): return copy.deepcopy(self.rows)
    def freeze(self, rows): pass
    def resize(self, rows, cols): pass
    def clear(self): self.rows = []
    def update_note(self, cell, note): self.note = note
    def get_note(self, cell): return self.note

class Sheet:
    def __init__(self):
        self.title, self.id, self.tabs = out.TITLE, 'test-private-workbook', []
        self.add_worksheet('Revenue Net Cash Ranked', 100, 11)
    def worksheets(self): return list(self.tabs)
    def worksheet(self, title):
        for tab in self.tabs:
            if tab.title == title: return tab
        raise MissingSheet(title)
    def add_worksheet(self, title, rows, cols, index=None):
        tab = Tab(title, len(self.tabs)+100)
        self.tabs.insert(len(self.tabs) if index is None else index, tab)
        return tab

def snapshot(sheet):
    return [(t.title, t.id, copy.deepcopy(t.rows), t.note) for t in sheet.tabs]

class OutputTests(unittest.TestCase):
    def setUp(self):
        self.fake = types.SimpleNamespace(WorksheetNotFound=MissingSheet)
        self.patch = patch.dict(sys.modules, {'gspread': self.fake})
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.sheet = Sheet()
        self.batch = date(2026, 10, 24)
        self.config = {'version': 1, 'cap_ratio': '0.10'}
    def publish(self, batch=None, symbols=('A',), config=None):
        return out.publish(self.sheet, batch or self.batch, [['Ticker', 'Name']]+[[s, s+' plc'] for s in symbols],
            [['Ticker', 'Status']]+[[s, 'match'] for s in symbols], {'status': 'complete'}, config or self.config,
            [['Ticker', 'Record', 'JSON', 'UTC']]+[[s, 'input', '{}', 'now'] for s in symbols])
    def receipt_edit(self, **changes):
        tab = self.sheet.worksheet('24-Oct-2026')
        receipt = json.loads(tab.note[len(out.PREFIX):]); receipt.update(changes)
        tab.note = out.PREFIX+json.dumps(receipt)
    def test_receipt_identity_hashes_seals_and_raw_position(self):
        receipt = self.publish()
        self.assertEqual([x.title for x in self.sheet.tabs[:2]], ['Revenue Net Cash Ranked', '24-Oct-2026'])
        self.assertEqual(receipt['row_count'], 1)
        self.assertEqual(receipt['spreadsheet_id'], self.sheet.id)
        self.assertEqual(receipt['tab_title'], '24-Oct-2026')
        for title in ('Coverage 24-Oct-2026', 'Inputs 24-Oct-2026'):
            self.assertTrue(self.sheet.worksheet(title).note.startswith(out.SEALED))
        self.assertEqual(out.resume_complete(self.sheet, self.batch, self.config), receipt)
    def test_exact_config_identity_and_row_count_required(self):
        for changes in ({'row_count': 2}, {'spreadsheet_id': 'other'}, {'tab_id': 999},
                        {'tab_title': 'wrong'}, {'status': 'partial'}, {'batch_date': '2026-10-25'},
                        {'criteria_hash': '0'*64}, {'inputs_hash': None}):
            self.sheet = Sheet(); self.publish(); self.receipt_edit(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                out.resume_complete(self.sheet, self.batch, self.config)
        self.sheet = Sheet(); self.publish()
        with self.assertRaises(ValueError): out.resume_complete(self.sheet, self.batch, {'version': 2})
    def test_config_hash_key_order_stable(self):
        self.assertEqual(out.stable_hash({'a': 1, 'b': 2}), out.stable_hash({'b': 2, 'a': 1}))
    def test_conflicting_republish_mutates_nothing(self):
        self.publish(); before = snapshot(self.sheet)
        with self.assertRaises(ValueError): self.publish(symbols=('B',))
        self.assertEqual(snapshot(self.sheet), before)
    def test_same_batch_idempotent_no_self_suppression(self):
        first = self.publish(); before = snapshot(self.sheet)
        self.assertEqual(self.publish(), first)
        self.assertEqual(snapshot(self.sheet), before)
    def test_companion_or_raw_tampering_fails(self):
        for title in ('24-Oct-2026', 'Coverage 24-Oct-2026', 'Inputs 24-Oct-2026'):
            self.sheet = Sheet(); self.publish()
            self.sheet.worksheet(title).rows[0][0] = 'tampered'
            with self.subTest(title=title), self.assertRaises(ValueError):
                out.resume_complete(self.sheet, self.batch, self.config)
    def test_unsealed_companion_fails(self):
        self.publish(); self.sheet.worksheet('Coverage 24-Oct-2026').note = out.STAGING+out.RUN_OWNER
        with self.assertRaises(ValueError): out.resume_complete(self.sheet, self.batch, self.config)
    def test_zero_results_still_has_sealed_inputs(self):
        receipt = self.publish(symbols=())
        self.assertEqual(receipt['row_count'], 0)
        self.assertTrue(receipt['inputs_hash'])
    def test_missing_inputs_rejected_before_write(self):
        before = snapshot(self.sheet)
        with self.assertRaises(ValueError):
            out.publish(self.sheet, self.batch, [['Ticker']], [['Ticker']], {'status':'complete'}, self.config)
        self.assertEqual(before, snapshot(self.sheet))
    def test_wrong_first_tab_fails_before_write(self):
        self.sheet.tabs[0].title = 'Unexpected'
        before = snapshot(self.sheet)
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(before, snapshot(self.sheet))
    def test_foreign_dated_output_not_adopted(self):
        tab = self.sheet.add_worksheet('24-Oct-2026', 100, 10); tab.note = out.STAGING+'other-run'
        before = snapshot(self.sheet)
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(before, snapshot(self.sheet))
    def test_calendar_cooldown_and_helper_exclusion(self):
        self.publish(date(2025, 10, 25), ('A',))
        helper = self.sheet.add_worksheet('Revenue Net Cash Metrics', 100, 52); helper.rows = [['Ticker'], ['B']]
        self.publish(date(2026, 10, 24), ('A', 'B'))
        self.assertEqual(self.sheet.worksheet('24-Oct-2026').rows[1:], [['B', 'B plc']])
        coverage = dict(self.sheet.worksheet('Coverage 24-Oct-2026').rows[1:])
        self.assertEqual(coverage['qualifying_rows_before_cooldown'], '2')
        self.assertEqual(coverage['cooldown_suppressed_rows'], '1')
        self.assertEqual(coverage['matched_rows'], '1')
        self.publish(date(2026, 10, 25), ('A',))
        self.assertEqual(self.sheet.worksheet('25-Oct-2026').rows[1:], [['A', 'A plc']])
    def test_leap_anniversary_clamps(self):
        self.assertEqual(appearance_eligible_on(date(2024,2,29)), date(2025,2,28))
        self.publish(date(2024,2,29), ('A',))
        self.publish(date(2025,2,27), ('A',))
        self.assertEqual(len(self.sheet.worksheet('27-Feb-2025').rows), 1)
        self.publish(date(2025,2,28), ('A',))
        self.assertEqual(len(self.sheet.worksheet('28-Feb-2025').rows), 2)
    def test_tampered_history_blocks_current_before_mutation(self):
        self.publish(date(2026,10,17), ('A',))
        self.sheet.worksheet('Inputs 17-Oct-2026').rows[0][0] = 'tampered'
        before = snapshot(self.sheet)
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(before, snapshot(self.sheet))
    def test_future_dated_history_fails(self):
        self.sheet.add_worksheet('31-Oct-2026',100,10)
        with self.assertRaises(ValueError): self.publish()
    def test_destination_exact_title_and_id(self):
        client = types.SimpleNamespace(open_by_key=lambda key: self.sheet)
        self.fake.service_account = lambda filename: client
        self.assertEqual(out.TITLE, '10% Rev & High GMs')
        self.assertIs(out.open_destination('not-read', self.sheet.id), self.sheet)
        with self.assertRaises(ValueError): out.open_destination('not-read', 'other-id')
        for title in ('Other workbook', 'Low Revenue Multiple & Net Cash Screener',
                      '10% Rev & High GMs ', '10% rev & high GMs'):
            self.sheet.title = title
            with self.subTest(title=title), self.assertRaises(ValueError):
                out.open_destination('not-read', self.sheet.id)

if __name__ == '__main__': unittest.main()

