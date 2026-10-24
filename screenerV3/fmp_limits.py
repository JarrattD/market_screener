"""Paced FMP budget shared by every client in one production event loop.

Cross-run exclusion is the unchanged production-screener GitHub concurrency
group, NOT this in-memory limiter. Production jobs wait 61 seconds before their
first FMP request so a preceding job's minute/bulk window can drain. Unrelated
account consumers are outside our control: 1,200 of Ultimate's 3,000 calls/min
remain unallocated. Do not run additional high-rate processes with this key.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

MAX_REQUESTS_PER_MINUTE = 1800.0
DEFAULT_REQUESTS_PER_MINUTE = 280.0
ORDINARY_BULK_INTERVAL = 10.1
PROFILE_ETF_BULK_INTERVAL = 61.0


def configured_rate(value=None):
    value = os.environ.get('FMP_REQUESTS_PER_MINUTE', DEFAULT_REQUESTS_PER_MINUTE) if value is None else value
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValueError('Invalid FMP request budget') from None
    if not math.isfinite(value) or not 0 < value <= MAX_REQUESTS_PER_MINUTE:
        raise ValueError('FMP request budget must be above zero and at most 1800/min')
    return value


def configured_concurrency(value=None):
    value = os.environ.get('FMP_CONCURRENCY', 10) if value is None else value
    try:
        workers = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Invalid FMP concurrency') from None
    if str(workers) != str(value) or not 1 <= workers <= 30:
        raise ValueError('FMP concurrency must be an integer from 1 to 30')
    return workers


def retry_delay(value, attempt):
    """Retry-After seconds or HTTP date; never shorten provider instructions."""
    seconds = None
    if value is not None:
        try:
            seconds = float(value)
            if not math.isfinite(seconds):
                seconds = None
        except (ValueError, TypeError):
            try:
                until = parsedate_to_datetime(value)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                seconds = (until - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    return max(1.0, seconds) if seconds is not None else min(120.0, 2.0 ** attempt)


def bulk_family(path, bulk=False):
    path = str(path).lower().split('?', 1)[0]
    is_bulk = 'bulk' in path or path.endswith('/shares_float/all') or bulk
    if not is_bulk:
        return None
    if 'profile' in path or 'etf' in path or (bulk and 'bulk' not in path and not path.endswith('/shares_float/all')):
        return 'profile_etf'
    return 'ordinary'


class RequestBudget:
    """Atomically pace all applicable buckets, with no bursts after a pause.

Sleep outside the lock so a newly received 429 can immediately extend the
deadline for every waiter. Recheck deadlines after every sleep; never reserve
future bulk slots before the global budget permits the actual request.
"""
    def __init__(self, requests_per_minute=DEFAULT_REQUESTS_PER_MINUTE, *,
                 clock=None, sleep=None):
        self.rate = configured_rate(requests_per_minute)
        self.interval = 60.0 / self.rate
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._next = 0.0
        self._bulk_next = 0.0
        self._profile_etf_next = 0.0
        self._cooldown_until = 0.0

    def restrict_rate(self, rate):
        # Sharing a loop never creates an extra quota; the strictest client wins.
        rate = configured_rate(rate)
        if rate < self.rate:
            self._next += 60.0 / rate - self.interval
            self.rate = rate
            self.interval = 60.0 / rate

    async def acquire(self, path='', *, bulk=False):
        family = bulk_family(path, bulk)
        while True:
            async with self._lock:
                now = self._clock()
                deadline = max(self._next, self._cooldown_until)
                if family:
                    deadline = max(deadline, self._bulk_next)
                if family == 'profile_etf':
                    deadline = max(deadline, self._profile_etf_next)
                delay = deadline - now
                if delay <= 0:
                    self._next = now + self.interval
                    if family:
                        self._bulk_next = now + ORDINARY_BULK_INTERVAL
                    if family == 'profile_etf':
                        self._profile_etf_next = now + PROFILE_ETF_BULK_INTERVAL
                    return
            await self._sleep(delay)

    async def cooldown(self, seconds):
        async with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)

    async def wait_for_cooldown(self):
        # Retain the serialized workflow slot on terminal quota failure. A later
        # job must not start just 61s after an unexpired 10-minute Retry-After.
        while True:
            async with self._lock:
                delay = self._cooldown_until - self._clock()
                if delay <= 0:
                    return
            await self._sleep(delay)


class SharedRequestLimiter:
    """Lazy proxy: construction is safe outside an event loop or in unit tests."""
    def __init__(self, requests_per_minute=None):
        self.rate = configured_rate(requests_per_minute)

    def _budget(self):
        loop = asyncio.get_running_loop()
        budget = getattr(loop, '_market_screener_fmp_budget', None)
        if budget is None:
            budget = RequestBudget(self.rate)
            setattr(loop, '_market_screener_fmp_budget', budget)
        else:
            budget.restrict_rate(self.rate)
        return budget

    async def acquire(self, path='', *, bulk=False):
        await self._budget().acquire(path, bulk=bulk)

    async def cooldown(self, seconds):
        await self._budget().cooldown(seconds)

    async def wait_for_cooldown(self):
        await self._budget().wait_for_cooldown()


def get_request_limiter(requests_per_minute=None):
    return SharedRequestLimiter(requests_per_minute)
