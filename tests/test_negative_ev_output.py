import sys
import types
import unittest
from datetime import date
from unittest.mock import patch
from screenerV3.negative_ev_output import publish, write_matrix, resume_complete
from screenerV3 import negative_ev_output as output

class MissingSheet(Exception):
    pass

class Tab:
    def __init__(self, title, ident): self.title=title; self.rows=[]; self.id=ident; self.note=''
    def update(self, range_name, values, value_input_option):
        offset=int(range_name[1:])-1
        self.rows[offset:offset+len(values)]=values
    def get_all_values(self): return self.rows
    def freeze(self, rows): pass
    def resize(self, rows, cols): pass
    def clear(self): self.rows=[]
    def update_note(self, cell, note): self.note=note
    def get_note(self, cell): return self.note

class Sheet:
    def __init__(self):
        self.tabs={}
        self.add_worksheet('Negative EV Ranked', 100, 10)
    def worksheets(self): return list(self.tabs.values())
    def worksheet(self, title):
        if title not in self.tabs: raise MissingSheet()
        return self.tabs[title]
    def add_worksheet(self, title, rows, cols, index=None):
        tab=Tab(title, len(self.tabs)+123)
        entries=list(self.tabs.items())
        entries.insert(len(entries) if index is None else index, (title, tab))
        self.tabs=dict(entries)
        return tab

class OutputTests(unittest.TestCase):
    def test_conflicting_publish_cannot_corrupt_completed_companions(self):
        batch = date(2026, 10, 24)
        rows = [["Ticker"], ["A"]]
        inputs = [["Ticker", "Kind", "JSON", "Time"], ["A", "input", "{}", "now"]]
        first = publish(self.sheet, batch, rows, [["Ticker"], ["A"]],
                        {"status": "complete", "matched_rows": 1}, {"rule": 1}, inputs)
        before = {name: (list(map(list, tab.rows)), tab.note) for name, tab in self.sheet.tabs.items()}
        with self.assertRaises(ValueError):
            publish(self.sheet, batch, [["Ticker"], ["B"]], [["Ticker"], ["B"]],
                    {"status": "complete", "matched_rows": 2}, {"rule": 1},
                    [["Ticker", "Kind", "JSON", "Time"], ["B", "input", "{}", "later"]])
        self.assertEqual(before, {name: (tab.rows, tab.note) for name, tab in self.sheet.tabs.items()})
        self.assertEqual(resume_complete(self.sheet, batch, {"rule": 1}), first)

    def test_completed_companions_are_sealed_and_tampering_blocks_resume(self):
        batch = date(2026, 10, 24)
        publish(self.sheet, batch, [["Ticker"]], [["Ticker"]], {"status": "complete"}, {"rule": 1})
        tab = self.sheet.tabs["Coverage 24-Oct-2026"]
        self.assertTrue(tab.note.startswith(output.SEALED))
        tab.note = output.STAGING + output.RUN_OWNER
        with self.assertRaises(ValueError): resume_complete(self.sheet, batch, {"rule": 1})

    def test_unmarked_and_foreign_dated_tabs_are_not_adopted(self):
        for note in ("", output.STAGING + "someone-else"):
            self.sheet = Sheet()
            tab = self.sheet.add_worksheet("24-Oct-2026", 100, 10)
            tab.rows = [["Ticker"]]
            tab.note = note
            with self.assertRaises(ValueError):
                publish(self.sheet, date(2026, 10, 24), [["Ticker"]], [["Ticker"]],
                        {"status": "complete"}, {"rule": 1})
            self.assertEqual(set(self.sheet.tabs), {"24-Oct-2026", "Negative EV Ranked"})
            self.assertEqual(tab.note, note)

    def setUp(self):
        self.patch=patch.dict(sys.modules, {'gspread':types.SimpleNamespace(WorksheetNotFound=MissingSheet)})
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.sheet=Sheet()

    def test_zero_results_publish_header_and_coverage(self):
        receipt=publish(self.sheet, date(2026,10,24), [['Ticker','Name']],
                        [['Ticker','Status'],['X','rejected']], {'status':'complete'}, {'rule':1})
        self.assertEqual(receipt['status'],'complete')
        self.assertIn('24-Oct-2026',self.sheet.tabs)
        self.assertIn('Coverage 24-Oct-2026',self.sheet.tabs)

    def test_chunked_readback_and_idempotent(self):
        rows=[['Ticker']]+[[str(i)] for i in range(1201)]
        tab=write_matrix(self.sheet,'test',rows)
        self.assertEqual(tab.rows,rows)
        self.assertIs(write_matrix(self.sheet,'test',rows),tab)
        tab.note = "verified-output"
        with self.assertRaises(ValueError): write_matrix(self.sheet,'test',[['different']])

    def test_readback_mismatch_blocks(self):
        with patch.object(Tab,'get_all_values',return_value=[['wrong']]):
            with self.assertRaises(ValueError): write_matrix(self.sheet,'test',[['right']])

if __name__=='__main__': unittest.main()


