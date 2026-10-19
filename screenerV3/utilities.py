from __future__ import annotations
import aiohttp
from dotenv import load_dotenv
import os
import json
import pandas as pd
from datetime import datetime, timezone
from urllib.parse import quote
from .negative_ev_provider import (FMPClient, FMPError, FMPAuthenticationError, Issue,
    normalize_profile, reconcile_cap_units, boolean)
from .data_freshness import DataFreshnessError, require_quote_current, require_statement_current
from .quote_refresh import refresh_quotes
from .legacy_statement_quality import resolve, annual_history
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from screener.Sheet import Sheet

load_dotenv()

class Handler:
    def __init__(self) -> None:
        self.api_key = os.environ['FMP_KEY']
        self.quality_audit = []
        self._profiles = {}
        self._client = FMPClient(self.api_key)
    
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
    
    def process_tickers(self, sheet_client:Sheet, path: str = None) -> dict[str:list]:
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
    
    def _record(self, ticker, status, reason, source="", source_date=""):
        self.quality_audit.append([ticker, status, reason, source, source_date,
                                   datetime.now(timezone.utc).isoformat()])

    async def _read(self, session, path, params=None):
        # Reuse the existing session and credentials, with one shared limiter.
        self._client._session = session
        return await self._client._request(path, params)

    async def get_profile(self, session: aiohttp.ClientSession, ticker: str):
        requested = set(ticker.split(","))
        rows = await self._read(session, "/api/v3/profile/" + quote(ticker, safe=",.-^"))
        profiles = {}
        for row in rows:
            symbol = row.get("symbol")
            if symbol not in requested:
                continue
            active = boolean(row.get("isActivelyTrading"))
            if active is False:
                self._record(symbol, "excluded_inactive", "Provider marks listing inactive", "FMP profile")
                continue
            if active is not True:
                self._record(symbol, "unresolved", "Active-company flag missing", "FMP profile")
                continue
            profiles[symbol] = normalize_profile(row)
        for symbol in requested - {row.get("symbol") for row in rows}:
            self._record(symbol, "unresolved", "Profile missing", "FMP profile")
        refreshed, issues = await refresh_quotes(self._client, profiles,
            normalize_profile=normalize_profile, reconcile_cap_units=reconcile_cap_units,
            issue_type=Issue, error_type=FMPError, auth_error_type=FMPAuthenticationError)
        if self._client.health.get("systemic_failure"):
            raise FMPError("Current quote acquisition failed; refusing an empty completed screen")
        for issue in issues:
            self._record(issue.symbol, "unresolved", issue.reason, "FMP quote")
        valid = []
        for symbol, row in refreshed.items():
            try:
                require_quote_current(row)
                if row.get("cap_amount") is None or row["cap_amount"] <= 0:
                    raise DataFreshnessError("Current quoted market cap missing or nonpositive")
            except DataFreshnessError as exc:
                self._record(symbol, "unresolved", str(exc), "FMP quote", row.get("as_of", ""))
                continue
            # Preserve the existing engine's field contract and investment rules.
            row["mktCap"] = row["cap_amount"]
            self._profiles[symbol] = row
            self._record(symbol, "quote_verified", "Active listing and dated quote verified", "FMP quote", row["as_of"])
            valid.append(row)
        return valid

    async def get_historical(self, session: aiohttp.ClientSession, ticker: str):
        # Historical prices are descriptive only; the current quote must have
        # been verified before either engine evaluates a market-cap ratio.
        async with session.get(f'https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}',
                               params={"apikey": self.api_key}) as response:
            if response.status in (401, 429):
                raise FMPError("historical_provider_auth_or_quota_failure", response.status)
            if response.status != 200:
                self._record(ticker, "unresolved", "Historical prices unavailable", "FMP historical prices")
                return None
            try:
                result = await response.json()
            except (ValueError, aiohttp.ClientError):
                self._record(ticker, "unresolved", "Historical response invalid", "FMP historical prices")
                return None
        history = result.get("historical", []) if isinstance(result, dict) else []
        try:
            if not history:
                raise DataFreshnessError("Historical price series missing")
            require_quote_current({"as_of": str(history[0].get("date", "")) + "T00:00:00+00:00"})
        except DataFreshnessError as exc:
            self._record(ticker, "unresolved", str(exc), "FMP historical prices")
            return None
        return result

    def _validate_financial(self, ticker, rows, label, annual=False):
        latest = None
        try:
            validated = annual_history(rows, ticker) if annual else [resolve(rows, ticker)]
            latest = validated[0]
            if latest is None:
                raise DataFreshnessError(f"{label} missing or ambiguous")
            require_statement_current(latest, label)
            profile = self._profiles.get(ticker, {})
            # The old engines assume matching monetary units. Do not silently
            # divide reporting-currency financials by a foreign-currency cap.
            if not latest.get("reportedCurrency") or latest["reportedCurrency"] != profile.get("cap_currency"):
                raise DataFreshnessError("Reporting/market-cap currencies differ or are missing; currency reconciliation required")
        except DataFreshnessError as exc:
            self._record(ticker, "unresolved", str(exc), label, latest.get("date", "") if latest else "")
            return None
        self._record(ticker, "financial_verified", label + " period verified", "FMP statement", latest["date"])
        return validated if annual else [latest] + [row for row in rows if row is not latest]

    async def get_balance_sheet(self, session: aiohttp.ClientSession, ticker: str):
        rows = await self._read(session, "/api/v3/balance-sheet-statement/" + quote(ticker, safe=""),
                                {"period": "quarter", "limit": 5})
        return self._validate_financial(ticker, rows, "Latest balance sheet")

    async def get_cashflow(self, session: aiohttp.ClientSession, ticker: str):
        rows = await self._read(session, "/api/v3/cash-flow-statement/" + quote(ticker, safe=""),
                                {"period": "annual", "limit": 5})
        return self._validate_financial(ticker, rows, "Annual cash flow", annual=True)

    async def get_key_metrics(self, session: aiohttp.ClientSession, ticker: str) -> str:
        """
        Retrieves the key metrics TTM (Trailing Twelve Months) for a given ticker.

        Parameters:
        - `session` (aiohttp.ClientSession): The aiohttp session to use for making requests.
        - `ticker` (str): The stock ticker symbol.

        Returns:
        - `str`: The key metrics TTM data in JSON format.
        """
        async with session.get(f'https://financialmodelingprep.com/api/v3/key-metrics-ttm/{ticker}?period=quarter&apikey={self.api_key}') as response:
            try:
                return await response.json()
            except Exception as e:
                pass
    
    async def get_floats(self) -> None:
        """
        Retrieves float data for all stocks.

        Returns:
        - `None`
        """
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://financialmodelingprep.com/api/v4/shares_float/all?apikey={self.api_key}") as response:
                try:
                    return await response.json()
                except Exception as e:
                    print(f"Error fetching floats: {e}")
                    return None
    
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