"""Offline quota/scheduling proofs: fake time, fake HTTP, no account access."""
import asyncio
import os
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

from screenerV3.fmp_limits import (
    RequestBudget, bulk_family, configured_rate, configured_concurrency,
    get_request_limiter, retry_delay,
)
from screenerV3.book_margin_provider import FMPClient as BookClient
from screenerV3.negative_ev_provider import FMPClient as NegativeClient


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        await asyncio.sleep(0)


class ManualClock(Clock):
    def __init__(self):
        super().__init__()
        self.waiters = []

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, future))
        await future

    def advance(self, now):
        self.now = now
        pending = []
        for due, future in self.waiters:
            if due <= now:
                if not future.done():
                    future.set_result(None)
            else:
                pending.append((due, future))
        self.waiters = pending


class Response:
    def __init__(self, status=200, body='[]', headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def text(self): return self.body


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def get(self, *_, **__):
        self.calls += 1
        return next(self.responses)


class LimitsTests(unittest.IsolatedAsyncioTestCase):
    def test_safe_defaults_opt_in_and_hard_ceiling(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_rate(), 280)
            self.assertEqual(configured_concurrency(), 10)
        with patch.dict(os.environ, FMP_REQUESTS_PER_MINUTE='1800', FMP_CONCURRENCY='30'):
            self.assertEqual(configured_rate(), 1800)
            self.assertEqual(configured_concurrency(), 30)
        for value in (0, -1, 1801, float('nan'), float('inf'), 'bad'):
            with self.assertRaises(ValueError): configured_rate(value)
        for value in (0, 31, 1.5, 'bad'):
            with self.assertRaises(ValueError): configured_concurrency(value)

    def test_bulk_families_do_not_depend_on_csv_flag(self):
        self.assertEqual(bulk_family('/stable/profile-bulk'), 'profile_etf')
        self.assertEqual(bulk_family('/stable/etf-holder-bulk'), 'profile_etf')
        self.assertEqual(bulk_family('/stable/income-statement-bulk'), 'ordinary')
        self.assertEqual(bulk_family('/api/v4/shares_float/all', True), 'ordinary')
        self.assertEqual(bulk_family('/new-unknown-csv', True), 'profile_etf')
        self.assertIsNone(bulk_family('/stable/batch-quote'))

    async def test_rolling_minute_pacing_and_no_catchup_burst(self):
        clock = Clock()
        budget = RequestBudget(1800, clock=clock.time, sleep=clock.sleep)
        stamps = []
        for _ in range(3601):
            await budget.acquire('/stable/profile')
            stamps.append(clock.now)
        self.assertAlmostEqual(stamps[-1], 120.0, places=7)
        self.assertTrue(all(b-a >= 1/30 - 1e-9 for a,b in zip(stamps, stamps[1:])))
        window = deque()
        for stamp in stamps:
            while window and stamp-window[0] >= 60-1e-8:
                window.popleft()
            window.append(stamp)
            self.assertLessEqual(len(window), 1800)
        clock.now += 500
        await budget.acquire()
        first = clock.now
        await budget.acquire()
        self.assertAlmostEqual(clock.now-first, 1/30)

    async def test_bulk_spacing_is_separate_from_ordinary_requests(self):
        clock = Clock()
        budget = RequestBudget(1800, clock=clock.time, sleep=clock.sleep)
        await budget.acquire('/stable/profile-bulk')
        await budget.acquire('/stable/profile')
        self.assertAlmostEqual(clock.now, 1/30)
        await budget.acquire('/stable/income-statement-bulk')
        self.assertAlmostEqual(clock.now, 10.1)
        await budget.acquire('/stable/etf-holder-bulk')
        self.assertAlmostEqual(clock.now, 61)
        await budget.acquire('/stable/profile-bulk')
        self.assertAlmostEqual(clock.now, 122)

    async def test_concurrent_clients_share_one_budget_and_strictest_rate(self):
        one, two = get_request_limiter(1800), get_request_limiter(600)
        self.assertIs(one._budget(), two._budget())
        self.assertEqual(one._budget().rate, 600)
        book, negative = BookClient('dummy', requests_per_minute=1800), NegativeClient('dummy', requests_per_minute=1800)
        self.assertIs(book.limiter._budget(), negative.limiter._budget())
        self.assertIs(book.limiter._budget(), one._budget())

    def test_production_environment_reaches_both_real_client_constructors(self):
        with patch.dict(os.environ, FMP_REQUESTS_PER_MINUTE='1800', FMP_CONCURRENCY='30'):
            for cls in (BookClient, NegativeClient):
                client = cls('dummy')
                self.assertEqual(client.limiter.rate, 1800)
                self.assertEqual(client.concurrency, 30)

    async def test_terminal_drain_waits_for_extended_provider_cooldown(self):
        clock = ManualClock()
        budget = RequestBudget(1800, clock=clock.time, sleep=clock.sleep)
        await budget.cooldown(600)
        waiting = asyncio.create_task(budget.wait_for_cooldown())
        await asyncio.sleep(0)
        clock.advance(61)
        await budget.cooldown(600)
        clock.advance(600)
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        clock.advance(661)
        await waiting

    async def test_provider_context_retains_cooldown_after_worker_failure(self):
        for cls in (BookClient, NegativeClient):
            client = cls('dummy')
            client._session = type('ClosingSession', (), {'close': AsyncMock()})()
            close = client._session.close
            release = asyncio.Event()
            client.limiter.wait_for_cooldown = release.wait
            exiting = asyncio.create_task(client.__aexit__(RuntimeError, RuntimeError('safe'), None))
            await asyncio.sleep(0)
            self.assertFalse(exiting.done())
            release.set()
            await exiting
            close.assert_awaited_once()
            self.assertIsNone(client._session)

    async def test_cooldown_interrupts_sleeping_waiter_and_cannot_be_shortened(self):
        clock = ManualClock()
        budget = RequestBudget(1800, clock=clock.time, sleep=clock.sleep)
        await budget.acquire()
        waiting = asyncio.create_task(budget.acquire())
        await asyncio.sleep(0)
        await budget.cooldown(90)
        await budget.cooldown(1)
        clock.advance(1/30)
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        clock.advance(89)
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        clock.advance(90)
        await waiting
        self.assertEqual(clock.now, 90)

    async def test_bulk_requests_cannot_bunch_up_behind_cooldown(self):
        clock = ManualClock()
        budget = RequestBudget(1800, clock=clock.time, sleep=clock.sleep)
        await budget.cooldown(90)
        stamps = []
        async def request():
            await budget.acquire('/stable/profile-bulk')
            stamps.append(clock.now)
        tasks = [asyncio.create_task(request()) for _ in range(2)]
        await asyncio.sleep(0)
        clock.advance(90)
        await asyncio.sleep(0)
        self.assertEqual(stamps, [90])
        clock.advance(151)
        await asyncio.gather(*tasks)
        self.assertEqual(stamps, [90, 151])

    async def test_cancelled_waiter_consumes_no_future_reservation(self):
        clock = ManualClock()
        budget = RequestBudget(1800, clock=clock.time, sleep=clock.sleep)
        await budget.acquire('/stable/profile-bulk')
        waiting = asyncio.create_task(budget.acquire('/stable/profile-bulk'))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError): await waiting
        clock.advance(61)
        await budget.acquire('/stable/profile-bulk')
        self.assertEqual(clock.now, 61)

    def test_retry_after_seconds_dates_and_backoff(self):
        self.assertEqual(retry_delay('600', 1), 600)
        self.assertEqual(retry_delay(None, 5), 32)
        self.assertEqual(retry_delay('nan', 1), 2)
        self.assertEqual(retry_delay('bad', 2), 4)
        future = format_datetime(datetime.now(timezone.utc)+timedelta(seconds=300))
        self.assertGreater(retry_delay(future, 1), 298)

    async def test_both_providers_budget_retries_and_cooldown_even_on_exhaustion(self):
        for cls in (BookClient, NegativeClient):
            with self.subTest(client=cls.__module__):
                client = cls('dummy', retries=1)
                client._session = Session([Response(429, headers={'Retry-After':'90'}), Response()])
                client.limiter.acquire, client.limiter.cooldown = AsyncMock(), AsyncMock()
                await client._request('/stable/profile-bulk', csv_response=True)
                self.assertEqual(client.limiter.acquire.await_count, 2)
                client.limiter.acquire.assert_awaited_with('/stable/profile-bulk', bulk=True)
                client.limiter.cooldown.assert_awaited_once_with(90)
                exhausted = cls('dummy', retries=0)
                exhausted._session = Session([Response(429, headers={'Retry-After':'120'})])
                exhausted.limiter.acquire, exhausted.limiter.cooldown = AsyncMock(), AsyncMock()
                with self.assertRaises(RuntimeError): await exhausted._request('/stable/profile')
                exhausted.limiter.cooldown.assert_awaited_once_with(120)
                with self.assertRaises(RuntimeError): await exhausted._request('/stable/profile')
                self.assertEqual(exhausted._session.calls, 1)


if __name__ == '__main__':
    unittest.main()
