import sys
import types
import unittest
from datetime import date
from unittest.mock import patch
from screenerV3.net_cash_output import publish, write_matrix

class MissingSheet(Exception):
    pass

class Tab:
    def __init__(self): self.rows=[]; self.id=123; self.note=''
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
    def __init__(self): self.tabs={}
    def worksheet(self, title):
        if title not in self.tabs: raise MissingSheet()
        return self.tabs[title]
    def add_worksheet(self, title, rows, cols):
        self.tabs[title]=Tab(); return self.tabs[title]

class OutputTests(unittest.TestCase):
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
