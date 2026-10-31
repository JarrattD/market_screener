"""Offline transport/concurrency regression tests; no API or Sheets credentials."""
import asyncio
import copy
import importlib
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from screenerV3.utilities import Handler, FMPTransportError


class FakeLimiter:
    def __init__(self):
        self.calls, self.cooldowns, self.drains = [], [], 0
    async def acquire(self, path, *, bulk=False):
        self.calls.append((path, bulk))
    async def cooldown(self, seconds):
        self.cooldowns.append(seconds)
    async def wait_for_cooldown(self):
        self.drains += 1


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.data, self.status, self.headers = data, status, headers or {}
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self):
        if isinstance(self.data, Exception): raise self.data
        return copy.deepcopy(self.data)


class Session:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception): raise response
        return response
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


class MainTransportTests(unittest.IsolatedAsyncioTestCase):
    def handler(self, **kwargs):
        with patch.dict('os.environ', {'FMP_KEY': 'not-a-real-key'}):
            return Handler(limiter=kwargs.pop('limiter', FakeLimiter()), **kwargs)

    async def test_production_environment_is_consumed_and_budget_is_shared_with_providers(self):
        from screenerV3.book_margin_provider import FMPClient
        with patch.dict('os.environ', FMP_KEY='not-a-real-key',
                        FMP_REQUESTS_PER_MINUTE='1800', FMP_CONCURRENCY='30'):
            handler = Handler()
            provider = FMPClient()
        self.assertEqual(handler.workers, 30)
        self.assertEqual(handler.limiter.rate, 1800)
        self.assertIs(handler.limiter._budget(), provider.limiter._budget())

    async def test_all_endpoint_shapes_and_floats_are_budgeted(self):
        handler = self.handler()
        session = Session([Response([]) for _ in range(6)])
        for method in ('get_profile', 'get_historical', 'get_balance_sheet', 'get_cashflow', 'get_key_metrics'):
            await getattr(handler, method)(session, 'TEST')
        with patch('screenerV3.utilities.aiohttp.ClientSession', return_value=session):
            await handler.get_floats()
        self.assertEqual(len(handler.limiter.calls), 6)
        self.assertEqual(handler.limiter.calls[-1], ('/api/v4/shares_float/all', True))
        self.assertEqual(session.calls[2][1]['params'], {'period': 'quarter', 'limit': 5, 'apikey': 'not-a-real-key'})
        self.assertEqual(session.calls[3][1]['params'], {'period': 'annual', 'limit': 5, 'apikey': 'not-a-real-key'})
        self.assertTrue(all(not kwargs['allow_redirects'] for _, kwargs in session.calls))
        self.assertTrue(all(kwargs['timeout'].total == 300 for _, kwargs in session.calls))

    async def test_429_retry_after_is_global_and_every_attempt_budgeted(self):
        handler = self.handler()
        session = Session([Response(status=429, headers={'Retry-After': '123'}), Response([{'ok': True}])])
        self.assertEqual(await handler.get_profile(session, 'TEST'), [{'ok': True}])
        self.assertEqual(handler.limiter.cooldowns, [123.0])
        self.assertEqual(len(handler.limiter.calls), 2)

    async def test_http200_quota_error_is_retried_and_exhaustion_fails_closed(self):
        handler = self.handler(retries=1)
        session = Session([Response({'Error Message': 'Limit Reach secret'}) for _ in range(2)])
        with self.assertRaisesRegex(FMPTransportError, r'^rate_limit_retries_exhausted \(HTTP 429\)$'):
            await handler.get_profile(session, 'TEST')
        self.assertEqual(handler.limiter.cooldowns, [60.0, 60.0])
        self.assertEqual(handler.limiter.drains, 1)
        with self.assertRaises(FMPTransportError):
            await handler.get_cashflow(session, 'NEXT')
        self.assertEqual(len(session.calls), 2)

    async def test_auth_failure_does_not_retry_or_leak_body(self):
        for response in (Response({'Error Message': 'Invalid API key secret'}), Response(status=401)):
            handler = self.handler()
            with self.assertRaisesRegex(FMPTransportError, r'^authentication_failed \(HTTP 401\)$'):
                await handler.get_profile(Session([response]), 'TEST')
            self.assertEqual(len(handler.limiter.calls), 1)

    async def test_5xx_and_timeout_retry_with_backoff(self):
        handler = self.handler()
        session = Session([Response(status=503), asyncio.TimeoutError('authenticated url secret'), Response([])])
        with patch('screenerV3.utilities.asyncio.sleep', new_callable=AsyncMock) as sleep:
            self.assertEqual(await handler.get_profile(session, 'TEST'), [])
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [2.0, 4.0])
        self.assertEqual(len(handler.limiter.calls), 3)

    async def test_bad_payload_redirect_and_http_failure_fail_closed(self):
        for response in (Response(data=ValueError('secret')), Response(status=302),
                         Response(status=403), Response('invalid'), Response({'message': 'secret error'})):
            with self.subTest(response=response):
                handler = self.handler()
                with self.assertRaises(FMPTransportError) as caught:
                    await handler.get_profile(Session([response]), 'TEST')
                self.assertNotIn('secret', str(caught.exception))
                self.assertEqual(len(handler.limiter.calls), 1)

    async def test_ordered_fetch_is_bounded_and_preserves_input_order(self):
        handler = self.handler(workers=3)
        active, peak, finished = 0, 0, []
        async def fetch(session, ticker):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep((3 - ticker % 3) * .001)
            finished.append(ticker)
            active -= 1
            return ticker * 2
        rows = [row async for row in handler.map_requests(None, fetch, range(8))]
        self.assertEqual(rows, [(i, i * 2) for i in range(8)])
        self.assertEqual(peak, 3)
        self.assertNotEqual(finished, list(range(8)))

    async def test_failed_chunk_cancels_and_joins_siblings(self):
        handler = self.handler(workers=3)
        completed = []
        async def fetch(session, ticker):
            try:
                if ticker == 1:
                    raise FMPTransportError('rate_limit_retries_exhausted', 429)
                await asyncio.sleep(30)
            finally:
                completed.append(ticker)
        with self.assertRaises(FMPTransportError):
            async for _ in handler.map_requests(None, fetch, range(20)):
                self.fail('No incomplete chunk should escape')
        self.assertCountEqual(completed, [0, 1, 2])
        self.assertEqual(handler.limiter.drains, 1)


SYMBOLS = ['PASS', 'PASS2', 'HIGHDEBT', 'NEGNCAV', 'LOWCF', 'NOHIST', 'NOFLOAT', 'NEGCF', 'FIN', 'CN']


class FixtureHandler(Handler):
    """Immutable fixture responses deliberately completed out of input order."""
    def __init__(self, workers=3):
        self.workers, self.calls = workers, []
        self.limiter = FakeLimiter()
    async def response(self, method, ticker, payload):
        self.calls.append((method, ticker))
        await asyncio.sleep((3 - SYMBOLS.index(ticker) % 3) * .0001)
        return copy.deepcopy(payload)
    async def get_profile(self, session, ticker):
        return await self.response('profile', ticker, [{
            'symbol': ticker, 'companyName': ticker, 'mktCap': 100,
            'country': 'CN' if ticker == 'CN' else 'GB', 'exchange': 'LSE',
            'industry': 'Banks' if ticker == 'FIN' else 'Software', 'lastDiv': 2}])
    async def get_balance_sheet(self, session, ticker):
        return await self.response('balance', ticker, [{
            'totalCurrentAssets': 10 if ticker == 'NEGNCAV' else 300,
            'totalLiabilities': 50, 'netDebt': 1 if ticker == 'HIGHDEBT' else -10}])
    async def get_cashflow(self, session, ticker):
        cf = -10 if ticker == 'NEGCF' else 5 if ticker == 'LOWCF' else 20
        return await self.response('cashflow', ticker, [
            {'freeCashFlow': cf, 'cashAtEndOfPeriod': 10, 'commonStockRepurchased': 0} for _ in range(5)])
    async def get_historical(self, session, ticker):
        return await self.response('historical', ticker, {
            'historical': [] if ticker == 'NOHIST' else [{'close': 10}, {'close': 20}]})
    async def get_key_metrics(self, session, ticker):
        return await self.response('metrics', ticker, [{
            'freeCashFlowPerShareTTM': 1, 'enterpriseValueTTM': 200, 'tangibleAssetValueTTM': 200}])
    async def get_floats(self):
        self.calls.append(('floats', 'all'))
        return [{'symbol': ticker, 'outstandingShares': 10} for ticker in SYMBOLS if ticker != 'NOFLOAT']


def screener_class(kind):
    # Only an import stub; constructors/Sheets are never invoked.
    try:
        import gspread
    except ModuleNotFoundError:
        with patch.dict(sys.modules, {'gspread': types.SimpleNamespace(Worksheet=object)}):
            return getattr(importlib.import_module('screenerV3.' + kind),
                           'PaybackScreener' if kind == 'payback_screener' else 'MultiMetricScreener')
    return getattr(importlib.import_module('screenerV3.' + kind),
                   'PaybackScreener' if kind == 'payback_screener' else 'MultiMetricScreener')


def make_screener(cls, workers=3):
    instance = cls.__new__(cls)
    instance.handler = FixtureHandler(workers)
    instance.tickers = {'GB': SYMBOLS}
    instance.profile_fstr_arr = SYMBOLS
    instance.results, instance.floats = {}, None
    return instance


class MainResultParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_outputs_match_frozen_serial_main_fixtures_exactly(self):
        golden = json.loads((Path(__file__).parent / 'fixtures' / 'main_v2_serial_results.json').read_text())
        for kind in ('payback_screener', 'multi_metric_screener'):
            for workers in (1, 3, 30):
                with self.subTest(kind=kind, workers=workers):
                    instance = make_screener(screener_class(kind), workers)
                    with redirect_stdout(io.StringIO()):
                        returned = await instance.run_async()
                    self.assertEqual(instance.results, golden[kind]['results'])
                    self.assertEqual(list(instance.results), golden[kind]['order'])
                    self.assertEqual(returned, golden[kind]['returned'])
                    self.assertCountEqual(instance.handler.calls, [tuple(x) for x in golden[kind]['calls']])

    async def test_payback_metric_transport_failure_cannot_become_default100(self):
        instance = make_screener(screener_class('payback_screener'))
        async def fail(session, ticker):
            raise FMPTransportError('rate_limit_retries_exhausted', 429)
        instance.handler.get_key_metrics = fail
        with redirect_stdout(io.StringIO()), self.assertRaises(FMPTransportError):
            await instance.run_async()
        self.assertEqual(instance.results, {})

    async def test_transport_failure_prevents_verified_completion_publish(self):
        from cloud_screener import main
        for kind in ('payback_screener', 'multi_metric_screener'):
            instance = make_screener(screener_class(kind))
            instance.sheet_client = types.SimpleNamespace(file=types.SimpleNamespace(id='fake-sheet'))
            async def fail(session, ticker):
                raise FMPTransportError('rate_limit_retries_exhausted', 429)
            instance.handler.get_profile = fail
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / 'completion.json'
                with patch('cloud_screener.resume_verified', return_value=None), \
                     patch('cloud_screener.london_batch_date', side_effect=AssertionError('Offline test must supply batch_date')), \
                     patch('cloud_screener.write_verified') as write, \
                     patch('cloud_screener.publish_manifest') as publish, \
                     redirect_stdout(io.StringIO()), self.assertRaises(FMPTransportError):
                    await main(sheet_factory=lambda _: instance.sheet_client,
                               screener_factories={'alpha': lambda: instance},
                               manifest_path=manifest, batch_date=date(2026, 10, 31))
                write.assert_not_called()
                publish.assert_not_called()
                self.assertFalse(manifest.exists())


class ProfileURILengthRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def handler(self, **kwargs):
        with patch.dict('os.environ', {'FMP_KEY': 'not-a-real-key'}):
            return Handler(limiter=FakeLimiter(), **kwargs)

    async def test_recursive_split_preserves_every_record_and_budgets_every_attempt(self):
        handler = self.handler()
        session = Session([
            Response(status=414), Response(status=414),
            Response([{'symbol': 'A'}]), Response([{'symbol': 'B'}]),
            Response([{'symbol': 'C'}, {'symbol': 'D'}]),
        ])
        actual = await handler.get_profile(session, 'A,B,C,D')
        self.assertEqual(actual, [{'symbol': s} for s in 'ABCD'])
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in session.calls],
                         ['A,B,C,D', 'A,B', 'A', 'B', 'C,D'])
        self.assertEqual(len(handler.limiter.calls), 5)
        self.assertEqual(handler.limiter.cooldowns, [])

    async def test_single_symbol_414_stops_without_recursive_retry(self):
        handler = self.handler()
        session = Session([Response(status=414)])
        with self.assertRaisesRegex(FMPTransportError, r'^http_error \(HTTP 414\)$'):
            await handler.get_profile(session, 'ONLY')
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(handler.limiter.calls), 1)

    async def test_other_client_errors_never_split_or_skip(self):
        for status in (400, 401, 402, 403, 404, 405, 413, 422):
            with self.subTest(status=status):
                handler = self.handler()
                session = Session([Response({'message': 'private body'}, status=status)])
                with self.assertRaises(FMPTransportError) as caught:
                    await handler.get_profile(session, 'A,B')
                self.assertEqual(caught.exception.status, status)
                self.assertIn(f'HTTP {status}', str(caught.exception))
                self.assertNotIn('private', str(caught.exception))
                self.assertEqual(len(session.calls), 1)

    async def test_first_or_second_half_failure_cannot_return_partial_results(self):
        for responses in (
            [Response(status=414), Response(status=403)],
            [Response(status=414), Response([{'symbol': 'A'}]), Response(status=403)],
            [Response(status=414), Response(status=414)],
        ):
            with self.subTest(requests=len(responses)):
                handler = self.handler()
                session = Session(responses)
                with self.assertRaises(FMPTransportError):
                    await handler.get_profile(session, 'A,B')
                self.assertEqual(len(session.calls), len(responses))
                self.assertEqual(len(handler.limiter.calls), len(responses))

    async def test_split_requires_valid_list_payloads(self):
        for left, right in (({}, []), ([], {})):
            handler = self.handler()
            session = Session([Response(status=414), Response(left), Response(right)])
            with self.assertRaisesRegex(FMPTransportError, '^invalid_profile_batch_shape$'):
                await handler.get_profile(session, 'A,B')

    async def test_quota_on_a_half_still_uses_shared_cooldown_not_splitting(self):
        handler = self.handler(retries=1)
        session = Session([Response(status=414),
                           Response(status=429, headers={'Retry-After': '123'}),
                           Response([{'symbol': 'A'}]), Response([{'symbol': 'B'}])])
        self.assertEqual(await handler.get_profile(session, 'A,B'),
                         [{'symbol': 'A'}, {'symbol': 'B'}])
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in session.calls],
                         ['A,B', 'A', 'A', 'B'])
        self.assertEqual(handler.limiter.cooldowns, [123.0])
        self.assertEqual(len(handler.limiter.calls), 4)

    async def test_non_profile_414_never_splits(self):
        handler = self.handler()
        session = Session([Response(status=414)])
        with self.assertRaises(FMPTransportError):
            await handler.get_historical(session, 'A,B')
        self.assertEqual(len(session.calls), 1)

    async def test_actual_universes_survive_simulated_uri_limit_without_loss_or_duplicates(self):
        root = Path(__file__).resolve().parents[1]
        for filename in ('cleaned_tickers.json', 'non_banking_tickers.json'):
            symbols = [symbol for group in json.loads((root / 'data' / filename).read_text()).values()
                       for symbol in group]
            handler = self.handler(workers=30)
            class LengthLimitedSession:
                calls, rejected = 0, 0
                def get(self, url, **kwargs):
                    self.calls += 1
                    # A synthetic key only; no production credentials or network.
                    length = len(url.encode('utf-8')) + len('?apikey=') + 32
                    if length > 8192:
                        self.rejected += 1
                        return Response(status=414)
                    return Response([{'symbol': s} for s in url.rsplit('/', 1)[-1].split(',')])
            session = LengthLimitedSession()
            batches = [','.join(symbols[i:i + 1000]) for i in range(0, len(symbols), 1000)]
            actual = []
            async for _, rows in handler.map_requests(session, handler.get_profile, batches):
                actual.extend(row['symbol'] for row in rows)
            with self.subTest(filename=filename):
                self.assertEqual(actual, symbols)
                self.assertGreater(session.rejected, 0)
                self.assertEqual(session.calls, len(handler.limiter.calls))
                self.assertLessEqual(session.calls, len(batches) + 2 * session.rejected)

    async def test_split_failure_never_publishes_or_writes_partial_screen(self):
        from cloud_screener import main
        handler = self.handler()
        session = Session([Response(status=414), Response([{'symbol': 'A'}]), Response(status=403)])
        sheet = types.SimpleNamespace(file=types.SimpleNamespace(id='fake-sheet'))
        async def run_async(debug=False):
            await handler.get_profile(session, 'A,B')
        screener = types.SimpleNamespace(sheet_client=sheet, run_async=run_async, results={})
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'completion.json'
            with patch('cloud_screener.resume_verified', return_value=None), \
                 patch('cloud_screener.london_batch_date', side_effect=AssertionError('Offline test must supply batch_date')), \
                 patch('cloud_screener.write_verified') as write, \
                 patch('cloud_screener.publish_manifest') as publish, \
                 self.assertRaises(FMPTransportError):
                await main(sheet_factory=lambda _: sheet,
                           screener_factories={'alpha': lambda: screener},
                           manifest_path=manifest, batch_date=date(2026, 10, 31))
            write.assert_not_called()
            publish.assert_not_called()
            self.assertFalse(manifest.exists())

    def test_status_diagnostics_never_include_invalid_status_objects(self):
        for status in ('secret', 'https://example.invalid/?apikey=secret', True, 999):
            self.assertEqual(str(FMPTransportError('http_error', status)), 'http_error')

    async def test_recursive_splitting_never_multiplies_worker_concurrency(self):
        handler = self.handler(workers=3)
        class ConcurrentSession:
            active, peak = 0, 0
            def get(self, url, **kwargs):
                owner = self
                suffix = url.rsplit('/', 1)[-1]
                class TrackedResponse(Response):
                    async def __aenter__(self):
                        owner.active += 1
                        owner.peak = max(owner.peak, owner.active)
                        await asyncio.sleep(.001)
                        return self
                    async def __aexit__(self, *args):
                        owner.active -= 1
                return TrackedResponse([{'symbol': suffix}], status=414 if ',' in suffix else 200)
        session = ConcurrentSession()
        batches = [f'A{i},B{i}' for i in range(7)]
        rows = [pair async for pair in handler.map_requests(session, handler.get_profile, batches)]
        self.assertEqual(rows, [(batch, [{'symbol': symbol} for symbol in batch.split(',')])
                                for batch in batches])
        self.assertEqual(session.peak, 3)
        self.assertEqual(session.active, 0)
        self.assertEqual(len(handler.limiter.calls), 21)

    async def test_duplicate_symbols_and_successful_payload_order_are_unchanged(self):
        handler = self.handler()
        session = Session([Response(status=414), Response([{'symbol': 'A'}]),
                           Response([{'symbol': 'B'}, {'symbol': 'A'}])])
        # Preserve the provider's order within each half, not a set or a re-sort.
        self.assertEqual(await handler.get_profile(session, 'A,A,B'),
                         [{'symbol': 'A'}, {'symbol': 'B'}, {'symbol': 'A'}])
        successful = [{'symbol': 'Z'}, {'symbol': 'A'}, {'symbol': 'Z'}]
        session = Session([Response(successful)])
        self.assertEqual(await handler.get_profile(session, 'Z,A,Z'), successful)
        self.assertEqual(len(session.calls), 1)

