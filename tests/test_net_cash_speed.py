from datetime import datetime, timezone
"""Request-count benchmarks are synthetic, never claimed as measured live savings."""
import unittest
from decimal import Decimal
from screenerV3.net_cash_calculations import combined_cash, number, gross_margin, evaluate, Criteria
from screenerV3.net_cash_data import FMPClient


async def verified_quote_fixture(client, profiles, **kwargs):
    return profiles, []


class OrderedGateTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, gated):
        client = FMPClient('dummy-test-key')
        calls = []
        async def request(path, params=None, **kwargs):
            symbol = path.rsplit('/',1)[-1]
            calls.append((symbol, path, params['period']))
            common = dict(symbol=symbol, date='2025-12-31', fiscalYear=2025, period='FY', reportedCurrency='GBP')
            if 'income-statement' in path:
                return [dict(common, revenue=100, grossProfit=20 if symbol=='GM_FAIL' else 30)]
            if 'balance-sheet' in path:
                return [dict(common, totalDebt=10, cashAndShortTermInvestments=10 if symbol=='CASH_FAIL' else 20,
                             totalStockholdersEquity=100, goodwillAndIntangibleAssets=20)]
            if 'cash-flow' in path:
                return [dict(common, fiscalYear=year, date=f'{year}-12-31', freeCashFlow=-10 if year==2021 else 15)
                        for year in range(2020,2026)]
            raise AssertionError(path)
        client._request = request
        gates = dict(income_gate=lambda i:gross_margin(i)>=Decimal('.30'),
                     balance_gate=lambda b:combined_cash(b)-number(b.get('totalDebt'),'debt')>0) if gated else {}
        result, issues = await client.fetch_financials(['GM_FAIL','CASH_FAIL','PASS'], include_cashflow=True, **gates)
        return client, result, calls, issues

    async def test_ordered_gates_reduce_requests_without_changing_survivors(self):
        old, baseline, before, _ = await self.collect(False)
        new, optimized, after, issues = await self.collect(True)
        self.assertEqual(len(before), 12)
        self.assertEqual(len(after), 8)
        self.assertEqual(sum(c[0]=='GM_FAIL' for c in after), 1)
        self.assertEqual(sum(c[0]=='CASH_FAIL' for c in after), 3)
        self.assertEqual(sum(c[0]=='PASS' for c in after), 4)
        self.assertFalse(issues)
        self.assertFalse(new.health['systemic_failure'])
        self.assertEqual(optimized['PASS'].cashflows, baseline['PASS'].cashflows)
        for f in [baseline['PASS'], optimized['PASS']]:
            result=evaluate(50,'GBP',f.balance,f.income,{},Criteria(cheapness_any=True),f.cashflows)
            self.assertTrue(result['matches'])
            self.assertEqual(result['cheapness_pass_count'],3)

    async def test_all_known_gross_margin_failures_are_valid_zero_results(self):
        client=FMPClient('dummy-test-key')
        calls=[]
        async def request(path,params=None,**kwargs):
            calls.append(path)
            return [dict(symbol='ONLY',period='FY',date='2025-12-31',revenue=100,grossProfit=29)]
        client._request=request
        result,_=await client.fetch_financials(['ONLY'],include_cashflow=True,
            income_gate=lambda i:gross_margin(i)>=Decimal('.30'))
        self.assertEqual(len(calls),1)
        self.assertFalse(client.health['systemic_failure'])
        self.assertIsNone(result['ONLY'].balance)

    async def test_unknown_gross_margin_not_conflated_with_known_failure(self):
        client=FMPClient('dummy-test-key')
        async def request(path,params=None,**kwargs):
            return [dict(symbol='ONLY',period='FY',date='2025-12-31',revenue=100,grossProfit=None)]
        client._request=request
        result,_=await client.fetch_financials(['ONLY'],include_cashflow=True,
            income_gate=lambda i:gross_margin(i)>=Decimal('.30'))
        self.assertTrue(client.health['systemic_failure'])
        self.assertEqual(client.metrics['early_base_failures'],0)


class EndToEndOrderedGateTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, *, gross=30, cash=20, debt=10, reporting='GBP', cashflows=True,
                       fail_balance=False):
        import contextlib
        import io
        from unittest.mock import patch
        import net_cash_screen
        from screenerV3.net_cash_data import FXRate, FMPError, Issue
        client=FMPClient('dummy-test-key')
        client.universe_source='synthetic'
        async def universe(): return [dict(symbol='ONLY')]
        async def profiles(universe):
            return {'ONLY':dict(symbol='ONLY',companyName='Synthetic',country='CN',cap_amount=50,
                                as_of=datetime.now(timezone.utc).isoformat(),cap_currency='GBP',security_eligible=True,security_identity='SYMBOL:ONLY')},[]
        async def fx(currencies):
            return {c:FXRate(c,1,'2026-10-19','2026-10-19','synthetic') for c in currencies if c=='GBP'},[
                Issue(c,'fx','unavailable') for c in currencies if c!='GBP']
        async def request(path,params=None,**kwargs):
            common=dict(symbol='ONLY',date='2025-12-31',fiscalYear=2025,period='FY',reportedCurrency=reporting)
            if 'income-statement' in path: return [dict(common,revenue=100,grossProfit=gross)]
            if 'balance-sheet' in path:
                if fail_balance and params['period']=='quarter': raise FMPError('provider_unavailable',500)
                return [dict(common,totalDebt=debt,cashAndShortTermInvestments=cash,
                             totalStockholdersEquity=100,goodwillAndIntangibleAssets=20)]
            if 'cash-flow' in path:
                return [dict(common,date=f'{y}-12-31',fiscalYear=y,freeCashFlow=10)
                        for y in range(2020,2026)] if cashflows else []
            raise AssertionError(path)
        client.fetch_universe=universe;client.fetch_profiles=profiles;client.fetch_fx=fx;client._request=request
        with patch.object(net_cash_screen,'refresh_quotes',verified_quote_fixture), patch.object(net_cash_screen,'publish',return_value={'verified':True}) as publish:
            with contextlib.redirect_stdout(io.StringIO()):
                try: await net_cash_screen.screen(client,object(),Criteria(cheapness_any=True))
                except ValueError as exc: return publish,None,exc
        return publish,publish.call_args.args,None

    async def test_all_margin_failures_publish_valid_zero(self):
        published,args,error=await self.run_case(gross=29)
        self.assertIsNone(error)
        self.assertEqual(args[4]['matched_rows'],0)
        self.assertEqual(args[4]['rejected'],1)

    async def test_all_unknown_margin_is_not_completed_zero(self):
        published,args,error=await self.run_case(gross=None)
        self.assertIsNotNone(error)
        published.assert_not_called()

    async def test_noncash_failure_does_not_require_reporting_fx(self):
        published,args,error=await self.run_case(cash=10,reporting='XYZ')
        self.assertIsNone(error)
        self.assertEqual(args[4]['rejected'],1)
        self.assertEqual(args[4]['matched_rows'],0)

    async def test_tbv_match_survives_missing_cashflow(self):
        published,args,error=await self.run_case(cashflows=False)
        self.assertIsNone(error)
        self.assertEqual(args[4]['matched_rows'],1)
        self.assertEqual(args[2][1][30],1)

    async def test_one_balance_endpoint_failure_is_not_a_match(self):
        published,args,error=await self.run_case(fail_balance=True)
        self.assertIsNotNone(error) # System-wide one-symbol fixture cannot claim complete zero.
        published.assert_not_called()

    async def test_negative_debt_remains_unresolved(self):
        published,args,error=await self.run_case(debt=-1)
        self.assertIsNone(error)
        self.assertEqual(args[4]['unresolved'],1)
        self.assertEqual(args[4]['matched_rows'],0)

if __name__=='__main__': unittest.main()
