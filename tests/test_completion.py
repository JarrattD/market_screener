import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from screenerV3.completion import write_verified, publish_manifest

D = date(2026, 10, 17)
DATA = {'ABC': {'Name': 'Example', 'FV Upside Metric': 50, '5Y Price Metric': 25, 'NCAV Ratio': .5, 'EV/aFCF': 3., 'Payback Rating': 2., '5Y average': .2, 'HQ Location': 'GB', 'P/TBV Ratio': .7}}
class Worksheet:
    id = 42
    def __init__(self, title, fail=False): self.title, self.values, self.fail = title, [], fail
    def update(self, **kw):
        self.values = copy.deepcopy(kw['values'])
        if self.fail: self.values.pop()
    def get_all_values(self, **kw): return self.values
class File:
    def __init__(self, id='sheet1', fail=False): self.id, self.tabs, self.fail = id, [], fail
    def worksheets(self): return self.tabs
    def add_worksheet(self, **kw):
        w = Worksheet(kw['title'], self.fail); self.tabs.append(w); return w
class Sheet:
    def __init__(self, **kw): self.file = File(**kw)
class CompletionTests(unittest.TestCase):
    def test_exact_rerun_reuses_tab_and_hash(self):
        s=Sheet(); a=write_verified(s,'alpha',DATA,D); b=write_verified(s,'alpha',DATA,D)
        self.assertEqual(a['content_sha256'],b['content_sha256']); self.assertEqual(len(s.file.tabs),1)
    def test_conflicting_tab_not_mutated(self):
        s=Sheet(); write_verified(s,'alpha',DATA,D); before=copy.deepcopy(s.file.tabs[0].values)
        changed=copy.deepcopy(DATA); changed['ABC']['Name']='Changed'
        with self.assertRaises(ValueError): write_verified(s,'alpha',changed,D)
        self.assertEqual(s.file.tabs[0].values,before)
    def test_readback_mismatch_fails(self):
        with self.assertRaises(ValueError): write_verified(Sheet(fail=True),'alpha',DATA,D)
    def test_empty_fails_before_create(self):
        s=Sheet()
        with self.assertRaises(ValueError): write_verified(s,'alpha',{},D)
        self.assertEqual(s.file.tabs,[])
    def test_both_receipts_required(self):
        a=write_verified(Sheet(),'alpha',DATA,D)
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'manifest.json'
            with self.assertRaises(ValueError): publish_manifest([a],D,p)
            self.assertFalse(p.exists())
    def test_success_persists_and_revision_stable(self):
        a=write_verified(Sheet(),'alpha',DATA,D); b=write_verified(Sheet(id='sheet2'),'beta',DATA,D)
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'manifest.json'; one=publish_manifest([a,b],D,p)
            a['verified_at']='new'; two=publish_manifest([b,a],D,p)
            self.assertEqual(one['revision_id'],two['revision_id']); self.assertTrue(p.exists())
    def test_wrong_date_or_same_sheet_fails(self):
        a=write_verified(Sheet(),'alpha',DATA,D); b=write_verified(Sheet(),'beta',DATA,D)
        with self.assertRaises(ValueError): publish_manifest([a,b],D)
        b['spreadsheet_id']='sheet2'; b['tab_title']='different'
        with self.assertRaises(ValueError): publish_manifest([a,b],D)
    def test_creation_error_propagates(self):
        s=Sheet()
        def fail(**kw): raise RuntimeError('permission')
        s.file.add_worksheet=fail
        with self.assertRaises(RuntimeError): write_verified(s,'alpha',DATA,D)
    def test_last_tab_not_selected(self):
        s=Sheet(); s.file.tabs=[Worksheet('old')]
        write_verified(s,'alpha',DATA,D)
        self.assertEqual(s.file.tabs[0].values,[])
    def test_partial_second_failure_never_publishes_and_can_resume(self):
        first, second = Sheet(), Sheet(id='sheet2',fail=True)
        receipt = write_verified(first,'alpha',DATA,D)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'manifest.json'
            with self.assertRaises(ValueError):
                other=write_verified(second,'beta',DATA,D)
                publish_manifest([receipt,other],D,path)
            self.assertFalse(path.exists())
            # Partial tab is deliberately held for explicit reconciliation, never cleared.
            with self.assertRaises(ValueError): write_verified(second,'beta',DATA,D)
            self.assertEqual(len(first.file.tabs),1)
            self.assertEqual(receipt['content_sha256'],write_verified(first,'alpha',DATA,D)['content_sha256'])
    def test_existing_tab_formatting_is_untouched(self):
        s=Sheet(); write_verified(s,'alpha',DATA,D)
        s.file.tabs[0].strikethrough=True
        write_verified(s,'alpha',DATA,D)
        self.assertTrue(s.file.tabs[0].strikethrough)
    def test_nonfinite_fails_before_create(self):
        s=Sheet(); bad=copy.deepcopy(DATA); bad['ABC']['NCAV Ratio']=float('nan')
        with self.assertRaises(ValueError): write_verified(s,'alpha',bad,D)
        self.assertEqual(s.file.tabs,[])
if __name__=='__main__': unittest.main()
