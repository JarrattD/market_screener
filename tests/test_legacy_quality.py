"""Offline regressions: quality exclusions do not change investment criteria."""
import asyncio
import os
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch
from screenerV3.legacy_quality_audit import result_tab

class LegacyQualityTests(unittest.IsolatedAsyncioTestCase):
    def handler(self):
        from screenerV3.utilities import Handler
        with patch.dict(os.environ,{'FMP_KEY':'dummy-offline'}):return Handler()

    async def test_inactive_and_unknown_are_distinct(self):
        h=self.handler()
        async def read(session,path,params=None):
            return [dict(symbol='OFF',isActivelyTrading=False),dict(symbol='UNKNOWN')]
        async def refresh(client,profiles,**kwargs):return profiles,[]
        h._read=read
        with patch('screenerV3.utilities.refresh_quotes',refresh):
            self.assertEqual(await h.get_profile(None,'OFF,UNKNOWN,MISSING'),[])
        status={r[0]:r[1] for r in h.quality_audit}
        self.assertEqual(status,{'OFF':'excluded_inactive','UNKNOWN':'unresolved','MISSING':'unresolved'})

    async def test_refresh_changes_cap_before_old_engine_gets_it(self):
        h=self.handler()
        async def read(session,path,params=None):
            return [dict(symbol='A',companyName='A',isActivelyTrading=True,isEtf=False,isFund=False,currency='GBP',mktCap=1000)]
        async def refresh(client,profiles,**kwargs):
            p=profiles['A'];p.update(cap_amount=50,cap_currency='GBP',as_of=datetime.now(timezone.utc).isoformat())
            return profiles,[]
        h._read=read
        with patch('screenerV3.utilities.refresh_quotes',refresh):
            rows=await h.get_profile(None,'A')
        self.assertEqual(rows[0]['mktCap'],50)
        self.assertTrue(any(r[1]=='quote_verified' for r in h.quality_audit))

    async def test_missing_quote_date_not_current(self):
        h=self.handler()
        async def read(session,path,params=None):
            return [dict(symbol='A',isActivelyTrading=True,isEtf=False,isFund=False,currency='GBP',mktCap=50)]
        async def refresh(client,profiles,**kwargs):return profiles,[]
        h._read=read
        with patch('screenerV3.utilities.refresh_quotes',refresh):self.assertEqual(await h.get_profile(None,'A'),[])
        self.assertTrue(any(r[1]=='unresolved' for r in h.quality_audit))

    async def test_financial_freshness_and_currency(self):
        h=self.handler();h._profiles={'A':{'cap_currency':'GBP'}}
        old=dict(symbol='A',date='2019-12-31',period='FY',reportedCurrency='GBP')
        self.assertIsNone(h._validate_financial('A',[old],'Annual cash flow',True))
        recent=dict(old,date=f'{datetime.now(timezone.utc).year-1}-12-31')
        history=[dict(recent,date=f'{datetime.now(timezone.utc).year-i}-12-31') for i in range(1,6)]
        self.assertIsNotNone(h._validate_financial('A',history,'Annual cash flow',True))
        self.assertIsNone(h._validate_financial('A',[dict(recent,reportedCurrency='USD')],'Annual cash flow',True))

    def test_audit_and_provisional_tabs_not_results(self):
        self.assertTrue(result_tab('17-Oct-2026'))
        for title in ['Data quality 17-Oct-2026','Provisional 17-Oct-2026','Guidance','Coverage 17-Oct-2026']:
            self.assertFalse(result_tab(title))

class PrivateAuditTests(unittest.TestCase):
    def test_quality_tabs_do_not_mutate_results(self):
        from types import SimpleNamespace
        from screenerV3.legacy_quality_audit import write_quality_audit
        class Tab:
            def __init__(self,title):self.title=title;self.rows=[];self.note=''
            def update(self,range_name,values,value_input_option):self.rows.extend(values)
            def get_all_values(self):return self.rows
            def update_note(self,cell,note):self.note=note
            def get_note(self,cell):return self.note
        class File:
            def __init__(self):self.tabs=[Tab('17-Oct-2026')]
            def worksheets(self):return self.tabs
            def add_worksheet(self,title,rows,cols):
                t=Tab(title);self.tabs.append(t);return t
        sheet=SimpleNamespace(file=File())
        original=sheet.file.tabs[0]
        rows=[['OLD','unresolved','Stale financials','FMP','2019-12-31','2026-10-19']]
        digest=write_quality_audit(sheet,'alpha',rows,date(2026,10,19))
        self.assertEqual(original.rows,[])
        self.assertEqual(sheet.file.tabs[1].title,'Data quality 19-Oct-2026')
        self.assertEqual(write_quality_audit(sheet,'alpha',rows,date(2026,10,19)),digest)
        with self.assertRaises(ValueError):write_quality_audit(sheet,'alpha',[],date(2026,10,19))

class LegacyRevisionTests(unittest.TestCase):
    def test_conflicting_cashflows_and_balance_values_unresolved(self):
        from screenerV3.legacy_statement_quality import resolve
        from screenerV3.data_freshness import DataFreshnessError
        for field in ('freeCashFlow','commonStockRepurchased','cashAtEndOfPeriod','totalCurrentAssets','totalLiabilities','netDebt'):
            a=dict(symbol='A',date='2025-12-31',period='FY',acceptedDate='2026-03-01T10:00:00Z',reportedCurrency='GBP')
            b=dict(a);a[field]=100;b[field]=-100
            for rows in ([a,b],[b,a]):
                with self.assertRaises(DataFreshnessError):resolve(rows,'A')
    def test_future_revision_unresolved(self):
        from screenerV3.legacy_statement_quality import resolve
        from screenerV3.data_freshness import DataFreshnessError
        with self.assertRaises(DataFreshnessError):resolve([dict(symbol='A',date='2025-12-31',acceptedDate='2099-01-01')],'A')
    def test_duplicate_annual_year_does_not_count_twice(self):
        from screenerV3.legacy_statement_quality import annual_history
        from screenerV3.data_freshness import DataFreshnessError
        rows=[dict(symbol='A',period='FY',date=f'{y}-12-31',freeCashFlow=10) for y in range(2022,2026)]
        with self.assertRaises(DataFreshnessError):annual_history(rows+[dict(rows[-1])],'A')

    def test_mixed_annual_currencies_unresolved(self):
        from screenerV3.legacy_statement_quality import annual_history
        from screenerV3.data_freshness import DataFreshnessError
        rows=[dict(symbol='A',period='FY',date=f'{y}-12-31',freeCashFlow=10,reportedCurrency='GBP') for y in range(2021,2026)]
        self.assertEqual(len(annual_history(rows,'A')),5)
        for currency in ('EUR',None):
            changed=[dict(r) for r in rows];changed[0]['reportedCurrency']=currency
            with self.assertRaises(DataFreshnessError):annual_history(changed,'A')
