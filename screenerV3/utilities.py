import asyncio
import aiohttp
from itertools import islice
from typing import TYPE_CHECKING
from dotenv import load_dotenv
import os
import json
import pandas as pd
from .fmp_limits import get_request_limiter, retry_delay, configured_concurrency
if TYPE_CHECKING:
    from .sheet import Sheet

load_dotenv()

class FMPTransportError(RuntimeError):
    """Sanitized transport failure; never includes a URL, key, or response body."""

    def __init__(self, reason, status=None):
        self.reason, self.status = reason, status
        # A numeric status is safe diagnostic context; never log response bodies
        # or URLs (which can contain the API key).
        status_text = f" (HTTP {status})" if type(status) is int and 100 <= status <= 599 else ""
        super().__init__(reason + status_text)


class Handler:
    def __init__(self, *, workers: int | None = None, limiter=None, retries: int = 3) -> None:
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 5:
            raise ValueError("retries must be an integer between 0 and 5")
        self.api_key = os.environ['FMP_KEY']
        self.workers = configured_concurrency(workers)
        self.limiter = limiter if limiter is not None else get_request_limiter()
        self.retries = retries
        self._fatal = None

    async def map_requests(self, session, method, tickers):
        """Fetch a bounded chunk concurrently, yield in original input order.

        No financial logic runs concurrently. At most ``workers`` fetch results
        (each may contain a metrics/cash-flow pair or a profile batch) are retained, and a failed request
        cancels/joins the remaining chunk before propagating to the caller.
        """
        iterator = iter(tickers)
        while batch := list(islice(iterator, self.workers)):
            tasks = [asyncio.create_task(method(session, ticker)) for ticker in batch]
            try:
                responses = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # A sibling failure must not cancel the task retaining a long
                # Retry-After and release the serialized production job early.
                await self.limiter.wait_for_cooldown()
                raise
            for ticker, response in zip(batch, responses):
                yield ticker, response

    async def get_fundamentals(self, session, ticker):
        # Retain each symbol's original key-metrics -> cash-flow request order.
        metrics = await self.get_key_metrics(session, ticker)
        cashflow = await self.get_cashflow(session, ticker)
        return metrics, cashflow
    
    def __read_json_file(self, file_path) -> dict[str:list]:
            """
            Reads a JSON file and returns its content as a dictionary.

            Parameters:
            - `file_path` (str): The path to the JSON file.

            Returns:
            - `dict`: A dictionary containing the content of the JSON file.
            """
            with open(file_path, 'r') as file:
                data = json.load(file)
            return data
    
    def process_tickers(self, sheet_client: "Sheet", path: str = None) -> dict[str:list]:
        """
        Processes tickers from a JSON file.

        Parameters:
        - `path` (str): The path to the JSON file containing stock tickers. Defaults to None.
        - `tickers` (dict): A dictionary of tickers. Defaults to None.

        Returns:
        - `dict`: A dictionary containing processed tickers.
        """
        t = self.__read_json_file(path)
        ret = {}
        previously_seen = sheet_client.get_all_previously_seen_tickers()
        removed = 0
        for k, v in t.items():
            init = len(v)
            ret[k]=[i for i in v if i not in previously_seen]
            removed += init-len(ret[k])
        
        print(f"{removed} tickers removed for being screened within the passed year.")
        return ret
    
    async def _raise_if_fatal(self):
        if self._fatal is not None:
            if self._fatal.status == 429:
                await self.limiter.wait_for_cooldown()
            raise self._fatal

    async def _request(self, session, path, params=None, *, bulk=False):
        request_params = dict(params or {})
        request_params['apikey'] = self.api_key
        for attempt in range(self.retries + 1):
            await self._raise_if_fatal()
            retry_after = None
            try:
                # Every HTTP attempt, including retries and floats, is budgeted.
                await self.limiter.acquire(path, bulk=bulk)
                await self._raise_if_fatal()
                async with session.get(
                        'https://financialmodelingprep.com' + path,
                        params=request_params, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=300)) as response:
                    status = response.status
                    retry_after = response.headers.get('Retry-After')
                    if status == 401:
                        raise FMPTransportError('authentication_failed', status)
                    if status == 429 or status >= 500:
                        raise FMPTransportError('rate_limit' if status == 429 else 'provider_unavailable', status)
                    if status >= 300:
                        raise FMPTransportError('unexpected_redirect' if status < 400 else 'http_error', status)
                    try:
                        payload = await response.json()
                    except (ValueError, aiohttp.ContentTypeError):
                        raise FMPTransportError('invalid_response_format') from None
                    if isinstance(payload, dict):
                        # FMP can return quota/authentication errors with HTTP200.
                        message = ' '.join(str(v) for k, v in payload.items()
                                           if str(k).lower() in ('error', 'error message', 'message')).lower()
                        if 'invalid api' in message or 'invalid key' in message:
                            raise FMPTransportError('authentication_failed', 401)
                        if any(term in message for term in ('limit reach', 'rate limit', 'too many requests')):
                            raise FMPTransportError('rate_limit', 429)
                        if message:
                            raise FMPTransportError('provider_error')
                    if not isinstance(payload, (list, dict)):
                        raise FMPTransportError('invalid_response_shape')
                    return payload
            except (aiohttp.ClientError, asyncio.TimeoutError):
                error = FMPTransportError('network_or_timeout')
            except FMPTransportError as error_:
                # A sibling already declared a fatal failure; do not turn it
                # back into a fresh retry/cooldown cycle.
                await self._raise_if_fatal()
                error = error_
                if error.status == 401:
                    self._fatal = error
                    raise error from None
                if error.status != 429 and not (error.status is not None and error.status >= 500):
                    raise error from None
            delay = retry_delay(retry_after, attempt + 1)
            if error.status == 429:
                # Stop every sibling on this account budget, including on the
                # final rejected attempt, not just this task's own retry.
                await self.limiter.cooldown(max(60.0, delay))
            if attempt == self.retries:
                self._fatal = FMPTransportError(error.reason + '_retries_exhausted', error.status)
                if error.status == 429:
                    await self.limiter.wait_for_cooldown()
                raise self._fatal from None
            if error.status != 429:
                await asyncio.sleep(delay)
        raise AssertionError('unreachable')

    async def get_profile(self, session: aiohttp.ClientSession, ticker: str):
        try:
            return await self._request(session, f'/api/v3/profile/{ticker}')
        except FMPTransportError as error:
            # Legacy 1,000-symbol batches can exceed the provider's URI limit.
            # Only an explicit 414 permits splitting: never reinterpret access,
            # authentication, quota or other failures as missing stock data.
            symbols = ticker.split(',')
            if error.status != 414 or len(symbols) < 2:
                raise
            midpoint = len(symbols) // 2
            # Sequential halves retain request/response order and the original
            # worker bound. Every attempt still consumes the shared budget.
            left = await self.get_profile(session, ','.join(symbols[:midpoint]))
            right = await self.get_profile(session, ','.join(symbols[midpoint:]))
            if not isinstance(left, list) or not isinstance(right, list):
                raise FMPTransportError('invalid_profile_batch_shape')
            # Do not expose partial results if either half failed.
            return left + right

    async def get_historical(self, session: aiohttp.ClientSession, ticker: str):
        return await self._request(session, f'/api/v3/historical-price-full/{ticker}')

    async def get_balance_sheet(self, session: aiohttp.ClientSession, ticker: str):
        return await self._request(session, f'/api/v3/balance-sheet-statement/{ticker}',
                                   {'period': 'quarter', 'limit': 5})

    async def get_cashflow(self, session: aiohttp.ClientSession, ticker: str):
        return await self._request(session, f'/api/v3/cash-flow-statement/{ticker}',
                                   {'period': 'annual', 'limit': 5})

    async def get_key_metrics(self, session: aiohttp.ClientSession, ticker: str):
        return await self._request(session, f'/api/v3/key-metrics-ttm/{ticker}',
                                   {'period': 'quarter'})

    async def get_floats(self):
        async with aiohttp.ClientSession() as session:
            return await self._request(session, '/api/v4/shares_float/all', bulk=True)

    def create_xlsx(self, file_path:str, results:dict) -> None:
        """
        Creates an Excel file with the screening results.

        Parameters:
        - `file_path` (str): The path to the Excel file.

        Returns:
        - `None`
        """
        df = pd.DataFrame.from_dict(results, orient='index')
        df.to_excel(file_path)
        print(f"File saved to {file_path}")
