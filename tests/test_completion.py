import copy
import asyncio
import json
import io
from unittest.mock import patch
from screenerV3.completion import london_batch_date, resume_verified
from cloud_screener import main
import tempfile
import unittest
from datetime import date
from pathlib import Path
from screenerV3.completion import write_verified, publish_manifest

D = date(2026, 10, 17)
DATA = {'ABC': {'Name': 'Example', 'FV Upside Metric': 50, '5Y Price Metric': 25, 'NCAV Ratio': .5, 'EV/aFCF': 3., 'Payback Rating': 2., '5Y average': .2, 'HQ Location': 'GB', 'P/TBV Ratio': .7}}
class Worksheet:
    id = 42
    def __init__(self, title, fail=False): self.title, self.values, self.fail, self.note = title, [], fail, ""
    def get_note(self, cell): return self.note
    def update_note(self, cell, content): self.note=content
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
            self.assertEqual(one,two); self.assertTrue(p.exists())
            raw=p.read_text()
            for private in ['spreadsheet_id','tab_id','row_count','content_sha256','verified_at','sheet1','sheet2']:
                self.assertNotIn(private,raw)
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
    def test_same_run_new_attempt_allowed_different_run_rejected(self):
        s=Sheet()
        with patch.dict('os.environ', {'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1'}):
            write_verified(s,'alpha',DATA,D)
        with patch.dict('os.environ', {'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'2'}):
            self.assertIsNotNone(resume_verified(s,'alpha',D))
        with patch.dict('os.environ', {'GITHUB_RUN_ID':'456'}):
            with self.assertRaises(ValueError): resume_verified(s,'alpha',D)
    def test_mixed_run_manifest_rejected(self):
        with patch.dict('os.environ', {'GITHUB_RUN_ID':'123'}): a=write_verified(Sheet(),'alpha',DATA,D)
        with patch.dict('os.environ', {'GITHUB_RUN_ID':'456'}):
            b=write_verified(Sheet(id='sheet2'),'beta',DATA,D)
            with self.assertRaises(ValueError): publish_manifest([a,b],D)
    def test_note_readback_failure_blocks(self):
        s=Sheet()
        original=s.file.add_worksheet
        def add(**kw):
            w=original(**kw)
            w.update_note=lambda cell,content: None
            return w
        s.file.add_worksheet=add
        with self.assertRaises(ValueError): write_verified(s,'alpha',DATA,D)
    def test_malformed_or_tampered_receipt_and_values_block(self):
        s=Sheet(); write_verified(s,'alpha',DATA,D); w=s.file.tabs[0]; original=w.note
        w.note='screener-completion-v1:{'
        with self.assertRaises(ValueError): resume_verified(s,'alpha',D)
        w.note=original.replace('"sheet1"','"another-sheet"')
        with self.assertRaises(ValueError): resume_verified(s,'alpha',D)
        w.note=original; w.values[1][1]='Tampered name'
        with self.assertRaises(ValueError): resume_verified(s,'alpha',D)
    def test_user_header_note_preserved(self):
        s=Sheet(); w=Worksheet('17-Oct-2026'); w.note='My private note'; s.file.tabs=[w]
        with self.assertRaises(ValueError): write_verified(s,'alpha',DATA,D)
        self.assertEqual(w.note,'My private note'); self.assertEqual(w.values,[])
    def test_nonfinite_fails_before_create(self):
        s=Sheet(); bad=copy.deepcopy(DATA); bad['ABC']['NCAV Ratio']=float('nan')
        with self.assertRaises(ValueError): write_verified(s,'alpha',bad,D)
        self.assertEqual(s.file.tabs,[])
class OrchestrationTests(unittest.TestCase):
    def test_actual_main_resumes_alpha_without_handler(self):
        sheets={'Screener':Sheet(), 'V2 Screener':Sheet(id='sheet2')}
        calls=[]
        class Screener:
            def __init__(self, kind):
                calls.append(kind)
                self.sheet_client=sheets['Screener' if kind=='alpha' else 'V2 Screener']
                # Model real Handler behavior: today's written tab removes prior selections.
                self.results={} if self.sheet_client.file.tabs else DATA
                self.kind=kind
            async def run_async(self, debug=False):
                if self.kind=='beta' and calls.count('beta')==1:
                    raise RuntimeError('transient upstream failure before beta write')
        async def pause(_): pass
        factories={k:(lambda k=k:Screener(k)) for k in ['alpha','beta']}
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'manifest.json'
            kwargs=dict(sheet_factory=sheets.__getitem__,screener_factories=factories,batch_date=D,manifest_path=path,pause=pause)
            with self.assertRaises(RuntimeError): asyncio.run(main(**kwargs))
            self.assertFalse(path.exists())
            self.assertEqual(calls,['alpha','beta'])
            sheets['Screener'].file.tabs[0].strikethrough=True
            asyncio.run(main(**kwargs))
            self.assertEqual(calls,['alpha','beta','beta'])
            self.assertTrue(sheets['Screener'].file.tabs[0].strikethrough)
            self.assertTrue(path.exists())
            asyncio.run(main(**kwargs))
            self.assertEqual(calls,['alpha','beta','beta'])
    def test_unmarked_tab_blocks_before_handler(self):
        s=Sheet(); s.file.tabs=[Worksheet('17-Oct-2026')]
        def forbidden(): self.fail('Handler must not be invoked')
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                asyncio.run(main(sheet_factory=lambda name:s,screener_factories={'alpha':forbidden,'beta':forbidden},batch_date=D,manifest_path=Path(td)/'m.json'))
    def test_rerun_date_uses_original_workflow_creation(self):
        with patch.dict('os.environ', {'GITHUB_RUN_ID':'123','GITHUB_REPOSITORY':'owner/repo'}):
            with patch('screenerV3.completion.urlopen', return_value=io.StringIO('{"id":123,"created_at":"2026-10-17T08:00:00Z"}')):
                self.assertEqual(london_batch_date(),D)
    def test_run_identity_lookup_failure_blocks(self):
        with patch.dict('os.environ', {'GITHUB_RUN_ID':'123','GITHUB_REPOSITORY':'owner/repo'}):
            with patch('screenerV3.completion.urlopen', side_effect=OSError('rate limit')):
                with self.assertRaises(OSError): london_batch_date()
    def test_manifest_rejects_shared_wrong_date(self):
        a=write_verified(Sheet(),'alpha',DATA,D); b=write_verified(Sheet(id='sheet2'),'beta',DATA,D)
        with self.assertRaises(ValueError): publish_manifest([a,b],date(2026,10,18))

if __name__=='__main__': unittest.main()
