"""Auditable FMP transport, universe, profiles and FX for the negative-EV screen.

Derived from the reviewed provider primitives in commit 036b0f5. This module
is intentionally independent of the unmerged acquisition-screen PR. It has no
income, size, margin or cash-flow screening gate.

This module does not choose investment thresholds or publish results. In
particular, a missing API value is never replaced with zero. Every unresolved
symbol is returned as an Issue for the caller's PRIVATE coverage report.
Nothing here prints symbols, counts, response bodies, or authenticated URLs.

Endpoints were checked against FMP's official documentation on 2026-10-19:
https://site.financialmodelingprep.com/developer/docs
https://site.financialmodelingprep.com/faqs?code=data
https://site.financialmodelingprep.com/developer/docs/changelog

Profile bulk has four parts (0..3), and a separate one-download/minute limit.
Legacy routes support the repository's existing eligible FMP account; stable
routes are also supported. Missing bulk/batch symbols are always reconciled
individually, never interpreted as non-matches.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Iterable, TypeVar
from urllib.parse import quote

import aiohttp

from .fmp_limits import configured_concurrency, get_request_limiter


BASE_URL = "https://financialmodelingprep.com"
UTC = timezone.utc
T = TypeVar("T")
U = TypeVar("U")
# These are PRICE units, not necessarily market-capitalization units.
SUBUNITS = {"GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01),
            "ZAc": ("ZAR", 0.01), "ZAC": ("ZAR", 0.01),
            "ILA": ("ILS", 0.01), "ILa": ("ILS", 0.01)}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def number(value: Any) -> float | None:
    """Parse finite provider numbers without conflating blank/null with zero."""
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
    if value in ("true", "1", 1):
        return True
    if value in ("false", "0", 0):
        return False
    return None


def currency_units(currency: str) -> tuple[str, float]:
    """Return base ISO currency and scale of a price/reporting unit."""
    currency = str(currency or "").strip()
    if currency in SUBUNITS:
        return SUBUNITS[currency]
    return currency.upper(), 1.0


@dataclass(frozen=True)
class Issue:
    symbol: str
    stage: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class FXRate:
    currency: str
    rate_to_gbp: float
    as_of: str
    fetched_at: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)




class FMPError(RuntimeError):
    """Safe to report: never contains a URL, key, or provider response body."""

    def __init__(self, reason: str, status: int | None = None):
        self.reason = reason
        self.status = status
        super().__init__(reason)


class FMPAuthenticationError(FMPError):
    pass


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    numeric = number(value)
    if numeric is not None:
        # FMP endpoints use both seconds and milliseconds.
        if numeric > 100_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, UTC)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None


def _retry_delay(value: str | None, attempt: int) -> float:
    seconds = number(value)
    if seconds is None and value:
        try:
            until = parsedate_to_datetime(value)
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            seconds = (until - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            pass
    # Do not shorten a provider's Retry-After instruction.
    return max(1.0, seconds) if seconds is not None else min(120.0, 2.0 ** attempt)


def _rows(body: str, allow_csv: bool = False) -> list[dict[str, Any]]:
    stripped = body.lstrip("\ufeff \r\n\t")
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        if allow_csv and stripped and not stripped.startswith("<"):
            reader = csv.DictReader(io.StringIO(stripped))
            if reader.fieldnames and "symbol" in reader.fieldnames:
                return list(reader)
        raise FMPError("invalid_response_format") from None
    if isinstance(payload, list) and all(isinstance(row, dict) for row in payload):
        return payload
    if isinstance(payload, dict):
        # Some FMP failures arrive as HTTP 200. Inspect only for classification;
        # never include raw provider text in an exception or diagnostic.
        message = " ".join(str(v) for k, v in payload.items()
                           if str(k).lower() in ("error", "error message", "message"))
        lower = message.lower()
        if "invalid api" in lower or "invalid key" in lower:
            raise FMPAuthenticationError("authentication_failed", 401)
        if any(term in lower for term in ("limit reach", "rate limit", "too many requests")):
            raise FMPError("rate_limit", 429)
        if "legacy" in lower and any(term in lower for term in ("no longer supported", "deprecated", "retired")):
            raise FMPError("legacy_endpoint_retired", 410)
        if any(term in lower for term in ("premium", "subscription", "restricted", "not available")):
            raise FMPError("endpoint_access_denied", 403)
        if message:
            raise FMPError("provider_error")
    raise FMPError("invalid_response_shape")


async def _map_bounded(items: Iterable[T], action: Callable[[T], Awaitable[U]],
                       workers: int) -> list[U]:
    """Process every item without creating tens of thousands of live tasks."""
    iterator = iter(enumerate(items))
    results: list[tuple[int, U]] = []

    async def worker() -> None:
        for index, item in iterator:
            results.append((index, await action(item)))

    tasks = [asyncio.create_task(worker()) for _ in range(workers)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return [result for _, result in sorted(results, key=lambda pair: pair[0])]


def normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy/stable names while retaining all original fields."""
    result = dict(profile)
    if result.get("marketCap") in (None, ""):
        result["marketCap"] = result.get("mktCap")
    for field in ("isEtf", "isFund", "isActivelyTrading", "isAdr"):
        result[field] = boolean(result.get(field))
    result["marketCap"] = number(result.get("marketCap"))
    result["price"] = number(result.get("price"))
    result["cap_amount"] = None
    result["cap_currency"] = None
    result["cap_currency_basis"] = "unresolved"
    raw_currency = str(result.get("currency") or "").strip()
    currency, scale = currency_units(raw_currency)
    if re.fullmatch(r"[A-Z]{3}", currency) and scale == 1:
        result["cap_amount"] = result["marketCap"]
        result["cap_currency"] = currency
        result["cap_currency_basis"] = "trading_currency"
    eligible, reason = security_eligibility(result)
    result["security_eligible"] = eligible
    result["security_reason"] = reason
    isin = str(result.get("isin") or "").strip().upper()
    result["security_identity"] = ("ISIN:" + isin if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin)
                                   else "SYMBOL:" + str(result.get("symbol") or ""))
    return result


def security_eligibility(profile: dict[str, Any]) -> tuple[bool | None, str]:
    """Classify, never drop, ordinary/ADR listings and explicit non-equities.

    No country, sector, exchange, liquidity, or prior-screening exclusions are
    made. REITs and financial companies are not excluded simply by industry.
    A missing eligibility fact is unknown, not silently false or true.
    """
    active = boolean(profile.get("isActivelyTrading"))
    etf = boolean(profile.get("isEtf"))
    fund = boolean(profile.get("isFund"))
    if active is False:
        return False, "inactive_listing"
    if etf is True or fund is True:
        return False, "fund_or_etf"
    instrument = str(profile.get("securityType") or profile.get("type") or "").strip().lower()
    instrument = re.sub(r"[ _-]", "", instrument)
    if instrument and instrument not in ("stock", "commonstock", "ordinaryshare", "ordinaryshares",
                                          "commonshares", "equity", "adr", "ads"):
        # Explicit non-stock metadata is more reliable than ticker suffixes.
        if instrument in ("etf", "fund", "mutualfund", "closedendfund", "warrant", "warrants",
                           "right", "rights", "unit", "units", "preferred", "preferredstock",
                           "preferredshares", "bond", "note", "trustpreferred", "index"):
            return False, "non_common_security_type"
        return None, "unrecognized_security_type"
    name = str(profile.get("companyName") or profile.get("name") or "")
    if re.search(r"\b(preferred (stock|shares|securities)|warrants?|subscription rights)\b", name, re.I):
        return False, "explicit_non_common_security_name"
    if active is None or etf is None or fund is None:
        return None, "missing_listing_eligibility_flags"
    return True, "active_operating_equity_or_adr"


def reconcile_cap_units(profile: dict[str, Any], stock_quote: dict[str, Any]
                        ) -> tuple[dict[str, Any], Issue | None]:
    """Disambiguate subunit caps against the SAME quote's price and shares.

    Dividing every GBp-labelled market cap by 100 is unsafe: a provider can
    quote the price in pence but already report market cap in pounds. A 15%
    tolerance allows asynchronous profile/quote refreshes; a discrepancy of
    100x cannot fit both alternatives. No stale weighted-average shares are
    substituted for current shares outstanding.
    """
    result = dict(profile)
    symbol = str(result.get("symbol") or "")
    currency, scale = currency_units(str(result.get("currency") or ""))
    cap = number(result.get("marketCap", result.get("mktCap")))
    price = number(stock_quote.get("price"))
    shares = number(stock_quote.get("sharesOutstanding"))
    if scale == 1:
        return result, None
    if not all(value is not None and value > 0 for value in (cap, price, shares)):
        return result, Issue(symbol, "market_cap_currency", "subunit_cap_needs_current_price_and_shares")
    quote_time = _timestamp(stock_quote.get("timestamp"))
    now = datetime.now(UTC)
    if quote_time is None or quote_time < now - timedelta(days=7) or quote_time > now + timedelta(hours=1):
        return result, Issue(symbol, "market_cap_currency", "subunit_cap_needs_recent_timestamped_quote")
    raw_product = price * shares
    base_product = raw_product * scale
    if abs(cap / base_product - 1) <= 0.15:
        result.update(cap_amount=cap, cap_currency=currency,
                      cap_currency_basis="whole_base_reconciled_price_times_shares")
    elif abs(cap / raw_product - 1) <= 0.15:
        result.update(cap_amount=cap * scale, cap_currency=currency,
                      cap_currency_basis="subunit_reconciled_price_times_shares")
    else:
        return result, Issue(symbol, "market_cap_currency", "subunit_cap_does_not_reconcile")
    result["cap_reconciliation_timestamp"] = stock_quote.get("timestamp")
    return result, None


def latest_statement(rows: list[dict[str, Any]], symbol: str,
                     annual_only: bool = False) -> dict[str, Any] | None:
    """Select the newest period, then newest restatement, never API list order."""
    candidates = []
    for row in rows:
        if str(row.get("symbol") or symbol) != symbol:
            continue
        period = str(row.get("period") or "").upper()
        if period not in ("FY", "ANNUAL", "Q1", "Q2", "Q3", "Q4", "H1", "H2"):
            continue
        if annual_only and period not in ("FY", "ANNUAL"):
            continue
        try:
            period_end = date.fromisoformat(str(row.get("date") or "")[:10])
        except ValueError:
            continue
        if period_end > datetime.now(UTC).date():
            continue
        if any((_timestamp(row.get(key)) is not None and
                _timestamp(row[key]) > datetime.now(UTC) + timedelta(hours=1))
               for key in ("acceptedDate", "filingDate", "fillingDate")):
            continue
        candidates.append(row)
    if not candidates:
        return None
    newest_end = max(str(row["date"])[:10] for row in candidates)
    newest = [row for row in candidates if str(row["date"])[:10] == newest_end]
    def revision(row):
        return _timestamp(row.get("acceptedDate") or row.get("filingDate") or row.get("fillingDate"))
    unknown = datetime.min.replace(tzinfo=UTC)
    # Equal-date conflicting balance revisions must not depend on API order.
    from decimal import Decimal, InvalidOperation
    def normalized(value):
        if value is None or value == "":
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return str(value)
    fields = ("totalDebt", "cashAndShortTermInvestments", "cashAndCashEquivalents", "shortTermInvestments")
    def signatures(records):
        return {(str(row.get("reportedCurrency") or ""),
                 *(normalized(row.get(key)) for key in fields)) for row in records}
    if any(revision(row) is None for row in newest) and len(signatures(newest)) > 1:
        return None
    latest_revision = max((revision(row) or unknown) for row in newest)
    newest = [row for row in newest if (revision(row) or unknown) == latest_revision]
    if len(signatures(newest)) > 1:
        return None
    return max(newest, key=lambda row: str(row.get("period") or "").upper() == "FY")


class FMPClient:
    """One run-scoped client. Use ``async with FMPClient() as client``.

    Constructing the client reads only the existing FMP_KEY environment value.
    It never discovers, prints, writes, or requests a credential. Tests can
    provide a dummy key and mock ``_request`` without making network calls.
    """

    def __init__(self, api_key: str | None = None, *, requests_per_minute: float | None = None,
                 concurrency: int | None = None, retries: int = 5, max_fx_age_days: int = 7,
                 use_profile_bulk: bool = True):
        self._api_key = api_key if api_key is not None else os.environ.get("FMP_KEY", "")
        if not self._api_key:
            raise FMPAuthenticationError("FMP_KEY_is_not_configured")
        concurrency = configured_concurrency(concurrency)
        if retries < 0 or max_fx_age_days < 1:
            raise ValueError("Invalid client configuration")
        self.concurrency = concurrency
        self.retries = retries
        self.max_fx_age_days = max_fx_age_days
        self.use_profile_bulk = use_profile_bulk
        self.limiter = get_request_limiter(requests_per_minute)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._session: aiohttp.ClientSession | None = None
        self._fatal: FMPError | None = None
        self._fx_quotes: dict[str, dict[str, Any]] = {}
        self._fx_loaded = False
        self._legacy_available = True
        self.diagnostics: list[Issue] = []
        self.health: dict[str, Any] = {"universe_available": False,
                                      "profile_data_available": None,
                                      "financial_data_available": None,
                                      "systemic_failure": False}
        self.metrics = {"http_requests": 0, "retry_requests": 0, "bulk_requests": 0}
        self.universe_source = ""
        self.universe_fetched_at = ""

    async def __aenter__(self) -> FMPClient:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300))
        return self

    async def __aexit__(self, *_: Any) -> None:
        try:
            await self.limiter.wait_for_cooldown()
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def _request(self, path: str, params: dict[str, Any] | None = None,
                       *, csv_response: bool = False) -> list[dict[str, Any]]:
        if self._session is None:
            raise RuntimeError("FMPClient must be used as an async context manager")
        request_params = dict(params or {})
        request_params["apikey"] = self._api_key
        for attempt in range(self.retries + 1):
            if self._fatal:
                raise self._fatal
            retry_after = None
            try:
                async with self._semaphore:
                    await self.limiter.acquire(path, bulk=csv_response)
                    if self._fatal:
                        raise self._fatal
                    self.metrics["http_requests"] += 1
                    self.metrics["retry_requests"] += int(attempt > 0)
                    self.metrics["bulk_requests"] += int(csv_response)
                    async with self._session.get(BASE_URL + path, params=request_params,
                                                 allow_redirects=False) as response:
                        retry_after = response.headers.get("Retry-After")
                        status = response.status
                        if status == 401:
                            raise FMPAuthenticationError("authentication_failed", status)
                        if status == 429 or status >= 500:
                            raise FMPError("rate_limit" if status == 429 else "provider_unavailable", status)
                        if status >= 400:
                            if status == 403 and path.startswith("/api/v3/"):
                                body = (await response.text()).lower()
                                if "legacy" in body and any(term in body for term in ("no longer supported", "deprecated", "retired")):
                                    raise FMPError("legacy_endpoint_retired", 410)
                            reason = ("endpoint_access_denied" if status in (402, 403) else
                                      "endpoint_unavailable" if status in (404, 410) else "http_error")
                            raise FMPError(reason, status)
                        if status >= 300:
                            raise FMPError("unexpected_redirect", status)
                        rows = _rows(await response.text(), allow_csv=csv_response)
                        return rows
            except FMPAuthenticationError as exc:
                self._fatal = exc
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # str(aiohttp exceptions) can contain the authenticated URL.
                error = FMPError("network_or_timeout")
            except FMPError as exc:
                if self._fatal is not None:
                    raise self._fatal
                if exc.status != 429 and not (exc.status is not None and exc.status >= 500):
                    raise
                error = exc
            delay = _retry_delay(retry_after, attempt + 1)
            if error.status == 429:
                await self.limiter.cooldown(max(60.0, delay))
            if attempt == self.retries:
                if error.status == 429:
                    # Avoid thousands more requests after a run-wide quota block.
                    self._fatal = FMPError("rate_limit_retries_exhausted", 429)
                    await self.limiter.wait_for_cooldown()
                    raise self._fatal
                raise FMPError(error.reason + "_retries_exhausted", error.status)
            if error.status != 429:
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _compatible(self, stable: str, legacy: str,
                          params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        # The existing application/account already uses the v3 routes. Do not
        # respond to a subscription/access denial by trying to obtain the same
        # denied data through a different route. A documented route retirement
        # or a missing URL is different: the stable replacement is supported.
        legacy_params = {key: val for key, val in (params or {}).items() if key != "symbol"}
        if not self._legacy_available:
            return await self._request(stable, params)
        try:
            return await self._request(legacy, legacy_params)
        except FMPAuthenticationError:
            raise
        except FMPError as exc:
            if self._fatal is not None:
                raise self._fatal
            if exc.status not in (404, 410):
                raise
            if exc.reason == "legacy_endpoint_retired":
                self._legacy_available = False
        return await self._request(stable, params)

    async def fetch_universe(self) -> list[dict[str, Any]]:
        """Fresh worldwide list; no country, size, coverage, or top-N filter.

        The legacy list supplies security type where the stable directory omits
        it. Its rows are unioned, not used as a prefiltered whitelist.
        """
        errors = []
        records: dict[str, dict[str, Any]] = {}
        sources = []
        for path in ("/stable/stock-list", "/api/v3/stock/list"):
            try:
                rows = await self._request(path)
            except FMPAuthenticationError:
                raise
            except FMPError as exc:
                if self._fatal is not None:
                    raise self._fatal
                errors.append(exc.reason)
                self.diagnostics.append(Issue("", "universe:" + path, exc.reason))
                continue
            if rows:
                sources.append(path)
            else:
                self.diagnostics.append(Issue("", "universe:" + path, "empty_directory_response"))
            for row in rows:
                symbol = str(row.get("symbol") or "").strip()
                if symbol:
                    records.setdefault(symbol, {}).update({k: v for k, v in row.items() if v not in (None, "")})
        if not records:
            self.health["systemic_failure"] = True
            raise FMPError("universe_unavailable:" + ",".join(errors or ["empty_response"]))
        self.health["universe_available"] = True
        self.universe_source = ";".join(sources)
        self.universe_fetched_at = utc_now()
        return [dict(records[symbol], symbol=symbol) for symbol in sorted(records)]

    async def fetch_profiles(self, universe: Iterable[dict[str, Any] | str]
                             ) -> tuple[dict[str, dict[str, Any]], list[Issue]]:
        """Retrieve every supplied symbol, reconciling partial bulk/batch data."""
        listing = {str(row if isinstance(row, str) else row.get("symbol") or ""): row
                   for row in universe}
        listing.pop("", None)
        profiles: dict[str, dict[str, Any]] = {}
        issues: list[Issue] = []

        def ingest(rows: list[dict[str, Any]], source: str) -> None:
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if symbol in listing:
                    original = listing[symbol]
                    base = dict(original) if isinstance(original, dict) else {}
                    base.update(row)
                    profile = normalize_profile(base)
                    stamp = _timestamp(profile.get("timestamp"))
                    profile.update(source=source, fetched_at=utc_now(),
                                   as_of=stamp.isoformat(timespec="seconds") if stamp else "")
                    profiles[symbol] = profile

        if self.use_profile_bulk:
            for part in range(4):
                try:
                    ingest(await self._request("/stable/profile-bulk", {"part": part}, csv_response=True),
                           "/stable/profile-bulk")
                except FMPAuthenticationError:
                    raise
                except FMPError as exc:
                    if self._fatal is not None:
                        raise self._fatal
                    self.diagnostics.append(Issue("", "profile_bulk", exc.reason))
                    if exc.status in (402, 403, 404, 410, 429):
                        break

        # Use the existing account's batch API before expensive symbol calls.
        missing = sorted(set(listing) - set(profiles))
        for offset in range(0, len(missing), 1000):
            batch = missing[offset:offset + 1000]
            try:
                path = "/api/v3/profile/" + quote(",".join(batch), safe=",.-^")
                ingest(await self._request(path), "/api/v3/profile")
            except FMPAuthenticationError:
                raise
            except FMPError as exc:
                if self._fatal is not None:
                    raise self._fatal
                self.diagnostics.append(Issue("", "profile_batch", exc.reason))
                if exc.status in (402, 403, 404, 410, 429):
                    if exc.reason == "legacy_endpoint_retired":
                        self._legacy_available = False
                    break

        async def one_profile(symbol: str) -> None:
            try:
                rows = await self._compatible("/stable/profile", "/api/v3/profile/" + quote(symbol, safe=""),
                                              {"symbol": symbol})
                ingest(rows, "/stable/profile_or_legacy")
                if symbol not in profiles:
                    issues.append(Issue(symbol, "profile", "missing_profile"))
            except FMPAuthenticationError:
                raise
            except FMPError as exc:
                if self._fatal is not None:
                    raise self._fatal
                issues.append(Issue(symbol, "profile", exc.reason))

        incomplete = {symbol for symbol, profile in profiles.items()
                      if profile.get("security_eligible") is None
                      or (profile.get("security_eligible") is True
                          and (profile.get("marketCap") is None or not profile.get("currency")))}
        await _map_bounded(sorted((set(listing) - set(profiles)) | incomplete),
                           one_profile, self.concurrency)

        for symbol, profile in profiles.items():
            if profile.get("security_eligible") is None:
                issues.append(Issue(symbol, "security_eligibility", profile["security_reason"]))
            if profile.get("security_eligible") is True and profile.get("marketCap") is None:
                issues.append(Issue(symbol, "market_cap", "missing_market_cap"))

        # Metadata/profile caps are never current-price evidence. Refresh every
        # eligible listing before downstream valuation or market-cap gates.
        from .quote_refresh import refresh_quotes
        profiles, quote_issues = await refresh_quotes(
            self, profiles, normalize_profile=normalize_profile,
            reconcile_cap_units=reconcile_cap_units, issue_type=Issue,
            error_type=FMPError, auth_error_type=FMPAuthenticationError)
        issues.extend(quote_issues)
        self.health["profile_data_available"] = bool(profiles)
        if listing and not profiles:
            self.health["systemic_failure"] = True
        elif profiles and all(profile.get("security_eligible") is None for profile in profiles.values()):
            self.health["systemic_failure"] = True
            self.diagnostics.append(Issue("", "profile", "all_listing_eligibility_metadata_unresolved"))
        return profiles, sorted(issues, key=lambda item: (item.symbol, item.stage, item.reason))

    async def fetch_fx(self, currencies: Iterable[str]
                       ) -> tuple[dict[str, FXRate], list[Issue]]:
        """Latest time-stamped FX to GBP, direct/inverse or USD cross.

        Rates older than max_fx_age_days, undated rates, and missing rates are
        unresolved rather than assigned 1. A cross records its oldest leg.
        Returned keys preserve the input units (e.g. GBp is 0.01 GBP).
        """
        fetched_at = utc_now()
        now = datetime.now(UTC)
        issues: list[Issue] = []
        result: dict[str, FXRate] = {}
        if not self._fx_loaded:
            try:
                rows = await self._compatible("/stable/batch-forex-quotes", "/api/v3/quotes/forex",
                                              {"short": "false"})
                self._fx_quotes.update({str(row.get("symbol")): row for row in rows if row.get("symbol")})
            except FMPAuthenticationError:
                raise
            except FMPError as exc:
                if self._fatal is not None:
                    raise self._fatal
                self.diagnostics.append(Issue("", "fx_bulk", exc.reason))
            self._fx_loaded = True

        def valid(row: dict[str, Any]) -> tuple[float, datetime] | None:
            price = number(row.get("price"))
            stamp = _timestamp(row.get("timestamp"))
            if price is None or price <= 0 or stamp is None:
                return None
            if stamp < now - timedelta(days=self.max_fx_age_days) or stamp > now + timedelta(hours=1):
                return None
            return price, stamp

        async def pair(base: str, target: str) -> tuple[float, datetime, str] | None:
            if base == target:
                return 1.0, now, "identity"
            for ticker, inverse in ((base + target, False), (target + base, True)):
                point = valid(self._fx_quotes.get(ticker, {}))
                if point is None:
                    try:
                        rows = await self._compatible("/stable/quote", "/api/v3/quote/" + ticker,
                                                      {"symbol": ticker})
                        row = next((row for row in rows if row.get("symbol") == ticker), {})
                        self._fx_quotes[ticker] = row
                        point = valid(row)
                    except FMPAuthenticationError:
                        raise
                    except FMPError:
                        if self._fatal is not None:
                            raise self._fatal
                        point = None
                if point:
                    value, stamp = point
                    return (1 / value if inverse else value), stamp, ticker
            return None

        # Sequential by currency avoids duplicate cross-rate refresh requests.
        for raw in sorted(set(str(currency or "").strip() for currency in currencies)):
            base, scale = currency_units(raw)
            if not re.fullmatch(r"[A-Z]{3}", base):
                issues.append(Issue(raw, "fx", "missing_or_invalid_currency"))
                continue
            if base == "GBP":
                result[raw] = FXRate(raw, scale, fetched_at, fetched_at, "GBP identity")
                continue
            direct = await pair(base, "GBP")
            if direct:
                value, stamp, source = direct
            else:
                left = await pair(base, "USD")
                right = await pair("USD", "GBP")
                if not left or not right:
                    issues.append(Issue(raw, "fx", "no_recent_timestamped_rate_to_GBP"))
                    continue
                value, stamp, source = left[0] * right[0], min(left[1], right[1]), left[2] + ";" + right[2]
            result[raw] = FXRate(raw, value * scale, stamp.isoformat(timespec="seconds"),
                                 fetched_at, "FMP " + source)
        return result, issues


