import unittest
from datetime import date, datetime, timezone
from screenerV3.net_cash_freshness import require_statement_current, require_quote_current, cutoff_months
from screenerV3.net_cash_calculations import MissingData

class FreshnessTests(unittest.TestCase):
    def test_calendar_grace_boundary(self):
        today=date(2026,10,19)
        self.assertEqual(cutoff_months(today,18),date(2025,4,19))
        require_statement_current({'date':'2025-04-19'},'Annual',today)
        with self.assertRaises(MissingData):require_statement_current({'date':'2025-04-18'},'Annual',today)
    def test_stale_provider_financials_unresolved(self):
        for year in (2004,2009,2011,2019):
            with self.assertRaises(MissingData):require_statement_current({'date':f'{year}-12-31'},'Annual',date(2026,10,19))
    def test_recent_crossmarket_periods(self):
        for stamp in ('2025-06-30','2025-12-31','2026-03-31','2026-06-30'):
            require_statement_current({'date':stamp},'Annual',date(2026,10,19))
    def test_quote_timestamp_not_retrieval_time(self):
        now=datetime(2026,10,19,tzinfo=timezone.utc)
        with self.assertRaises(MissingData):require_quote_current({'fetched_at':now.isoformat()},now)
        require_quote_current({'as_of':'2026-10-16T16:30:00Z'},now)
        with self.assertRaises(MissingData):require_quote_current({'as_of':'2026-10-01T16:30:00Z'},now)
        with self.assertRaises(MissingData):require_quote_current({'as_of':'2026-10-20T16:30:00Z'},now)
    def test_end_month_cutoff(self):
        self.assertEqual(cutoff_months(date(2026,8,31),18),date(2025,2,28))

class FreshQuotePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_refresh_precedes_cap_gate_and_records_date(self):
        import contextlib,io
        from unittest.mock import patch
        import net_cash_screen
        from screenerV3.net_cash_data import FMPClient,FXRate,normalize_profile
        from screenerV3.net_cash_calculations import Criteria
        client=FMPClient('dummy-offline');client.universe_source='synthetic'
        now=int(datetime.now(timezone.utc).timestamp())
        async def universe():return [{'symbol':'DROP'}]
        async def profiles(universe):
            return {'DROP':normalize_profile(dict(symbol='DROP',marketCap=100000000,currency='GBP',price=100,
                isActivelyTrading=True,isEtf=False,isFund=False,companyName='Example'))},[]
        async def fx(currencies):return {c:FXRate(c,1,'2026-10-19','2026-10-19','synthetic') for c in currencies},[]
        calls=[]
        async def request(path,params=None,**kwargs):
            calls.append(path)
            common=dict(symbol='DROP',date='2025-12-31',period='FY',reportedCurrency='GBP')
            if path=='/stable/batch-quote':return [dict(symbol='DROP',price=50,marketCap=50000000,timestamp=now)]
            if 'income-statement' in path:return [dict(common,revenue=100,grossProfit=40)]
            if 'balance-sheet' in path:return [dict(common,totalDebt=0,cashAndShortTermInvestments=10000000,totalStockholdersEquity=60000000,goodwillAndIntangibleAssets=0)]
            if 'cash-flow' in path:return []
            raise AssertionError(path)
        client.fetch_universe=universe;client.fetch_profiles=profiles;client.fetch_fx=fx;client._request=request
        with patch.object(net_cash_screen,'publish',return_value={'ok':True}) as publish,contextlib.redirect_stdout(io.StringIO()):
            await net_cash_screen.screen(client,object(),Criteria(cheapness_any=True))
        rows=publish.call_args.args[2]
        self.assertEqual(len(rows),2)
        self.assertEqual(float(rows[1][4]),50000000)
        self.assertTrue(rows[1][16])
        self.assertEqual(calls[0],'/stable/batch-quote')

class FutureRevisionTests(unittest.TestCase):
    def test_future_filing_does_not_override_known_account(self):
        from screenerV3.net_cash_data import latest_statement,five_year_cashflows
        row=dict(symbol='A',date='2025-12-31',period='FY',freeCashFlow=10,acceptedDate='2099-01-01')
        self.assertIsNone(latest_statement([row],'A'))
        self.assertEqual(five_year_cashflows([row],'A')[1],'future_annual_revision')
