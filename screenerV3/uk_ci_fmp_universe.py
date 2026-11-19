"""UK listings FMP marks active; operational HQ and coverage gaps are separate."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re

from uk_ci_universe import ISIN, Listing, _name_key
from .negative_ev_provider import FMPError, _map_bounded, boolean

SCREENER_DOC = "https://site.financialmodelingprep.com/developer/docs/stable/search-company-screener"
SCREEN_ROUTE = "/stable/company-screener"
PROFILE_ROUTE = "/stable/profile"
BULK_ROUTE = "/stable/profile-bulk"
LSE_UNSPLIT = "LSE (segment not supplied)"
MARKETS = ("Main Market", "AIM", LSE_UNSPLIT, "Aquis")
VENUE_CODES = {
    "LSE": LSE_UNSPLIT, "LONDON STOCK EXCHANGE": LSE_UNSPLIT, "XLON": LSE_UNSPLIT,
    "AIM": "AIM", "AIMX": "AIM", "ALTERNATIVE INVESTMENT MARKET": "AIM",
    "LONDON STOCK EXCHANGE AIM": "AIM", "LONDON STOCK EXCHANGE - AIM": "AIM",
    "AIM MARKET": "AIM",
    "MAIN MARKET": "Main Market", "LONDON STOCK EXCHANGE MAIN MARKET": "Main Market",
    "LONDON STOCK EXCHANGE - MAIN MARKET": "Main Market",
    "AQSE": "Aquis", "AQUIS": "Aquis", "AQUIS STOCK EXCHANGE": "Aquis", "AQSG": "Aquis",
    "AQUIS STOCK EXCHANGE PLC": "Aquis",
    "AQUIS STOCK EXCHANGE GROWTH MARKET": "Aquis",
}
SYMBOL = re.compile(r"[A-Z0-9.^_-]{1,40}\Z")
PAGE_SIZE = 1000
MAX_PAGES = 25
# The provider's validated aliases are queried without assuming all return data.
EXCHANGE_FILTERS = ("LSE", "AIM", "AQSE", "AQUIS")


@dataclass(frozen=True)
class FMPListing(Listing):
    @property
    def issuer_key(self) -> str:
        return self.isin or f"FMP:{self.fmp_symbol}"


def _text(value) -> str:
    return str(value or "").strip()


def _fields(row, keys) -> set[str]:
    return {_text(row.get(key)).upper() for key in keys if _text(row.get(key))}


def _venue(row):
    labels = _fields(row, ("exchangeShortName", "exchange", "exchangeFullName",
                           "marketSegment", "segment", "market", "mic"))
    markets = {VENUE_CODES[label] for label in labels if label in VENUE_CODES}
    if len(markets) > 1:
        markets.discard(LSE_UNSPLIT)
    return next(iter(markets)) if len(markets) == 1 else ""


def _non_equity(row):
    if boolean(row.get("isEtf")) is True:
        return True
    types = _fields(row, ("type", "securityType", "instrumentType", "mifirType"))
    return bool(types & {"ETF", "ETFS", "BOND", "BONDS", "DEBT", "ETN", "ETC", "WARRANT", "WARRANTS"})


def _kind(row):
    name = _text(row.get("companyName") or row.get("name"))
    industry = _text(row.get("industry"))
    if re.search(r"\bREITS?\b|\breal estate investment trust\b", f"{name} {industry}", re.I):
        return "REIT"
    if boolean(row.get("isFund")) is True or re.search(
        r"\binvestment trust\b|\bclosed[- ]end(?:ed)?(?: investment)? fund\b", f"{name} {industry}", re.I
    ):
        return "Fund"
    return "Company"


def _canonical_symbol(row):
    symbol = _text(row.get("symbol")).upper()
    return symbol if SYMBOL.fullmatch(symbol) else ""


def _ticker(symbol):
    return re.sub(r"\.(?:L|IL|AQ|AQSE)\Z", "", symbol)


def _fullest(rows):
    return max(rows, key=lambda row: sum(v not in ("", None) for v in row.values()))


def _latest_identity(rows, symbol):
    """Conflicting provider identity or headquarters values do not pick a winner."""
    if not rows:
        return None
    identity_keys = ("isin", "currency", "country", "address", "zip")
    if any(len({value for row in rows for value in _fields(row, (key,))}) > 1
           for key in identity_keys):
        return None
    best = _fullest(rows)
    chosen_market = _venue(best)
    if not chosen_market or _canonical_symbol(best) != symbol:
        return None
    markets = {_venue(row) for row in rows} - {"", LSE_UNSPLIT}
    if len(markets) > 1:
        return None
    return best


async def _screen(client, exchange):
    seen = set()
    rows = []
    for page in range(MAX_PAGES):
        batch = await client._request(SCREEN_ROUTE, {
            "exchange": exchange, "isActivelyTrading": "true", "isEtf": "false",
            "includeAllShareClasses": "true", "limit": PAGE_SIZE, "page": page,
        })
        if not batch:
            return rows
        sig = tuple(sorted(str(row.get("symbol") or "") for row in batch))
        if sig in seen:
            raise FMPError("fmp_uk_screener_repeated_page")
        seen.add(sig)
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
    raise FMPError("fmp_uk_screener_pagination_incomplete")


async def _profiles(client, candidates, stats):
    found = defaultdict(list)
    if getattr(client, "use_profile_bulk", True):
        for part in range(4):
            try:
                batch = await client._request(BULK_ROUTE, {"part": part}, csv_response=True)
            except FMPError as exc:
                if getattr(client, "_fatal", None) or exc.status == 401:
                    raise
                stats["bulk_profile_errors"] += 1
                if exc.status in (402, 403, 404, 410, 429):
                    break
                continue
            for row in batch:
                symbol = _canonical_symbol(row)
                if (symbol and symbol not in candidates and _venue(row) in {"AIM", "Aquis"}
                        and boolean(row.get("isActivelyTrading")) is True and not _non_equity(row)):
                    candidates[symbol] = row
                    stats["additional_aim_aquis_symbols_from_profiles"] += 1
                if symbol in candidates:
                    found[symbol].append(row)

    selected = {}
    for symbol, group in found.items():
        profile = _latest_identity(group, symbol)
        if profile and ISIN.fullmatch(_text(profile.get("isin")).upper()):
            selected[symbol] = profile

    # Individual calls are needed only for bulk coverage gaps; no foreign symbols
    # outside the FMP UK venue screener are looked up.
    async def one(symbol):
        try:
            batch = await client._request(PROFILE_ROUTE, {"symbol": symbol})
        except FMPError as exc:
            if getattr(client, "_fatal", None) or exc.status == 401:
                raise
            stats["individual_profile_errors"] += 1
            return
        match = _latest_identity([row for row in batch if _canonical_symbol(row) == symbol], symbol)
        if match and ISIN.fullmatch(_text(match.get("isin")).upper()):
            selected[symbol] = match
    missing = sorted(set(candidates) - set(selected))
    # Probe one ordinary profile before fanout when bulk returned no usable data:
    # if the underlying endpoint itself is unavailable, don't issue thousands of doomed calls.
    if missing and not selected:
        try:
            probe = await client._request(PROFILE_ROUTE, {"symbol": missing[0]})
        except FMPError as exc:
            if getattr(client, "_fatal", None) or exc.status in (401, 402, 403, 429):
                raise
        else:
            matched = _latest_identity([row for row in probe if _canonical_symbol(row) == missing[0]], missing[0])
            if matched and ISIN.fullmatch(_text(matched.get("isin")).upper()):
                selected[missing.pop(0)] = matched
    await _map_bounded(missing, one, client.concurrency)
    return selected


def _prefer(member):
    listing, profile = member
    completeness = sum(bool(profile.get(k)) for k in ("address", "city", "zip", "country", "description", "currency"))
    return (not bool(profile), boolean(profile.get("isAdr")) is True,
            -completeness, 0 if listing.market in ("Main Market", "AIM") else 1, listing.fmp_symbol)


def _assemble(screen_rows, profile_rows, stats, snapshot):
    entries = []
    for symbol, base in screen_rows.items():
        market = _venue(base)
        profile = profile_rows.get(symbol, {})
        normalized = {**base, **{k: v for k, v in profile.items() if v not in ("", None)}, "symbol": symbol}
        if _non_equity(normalized) or boolean(normalized.get("isActivelyTrading")) is False:
            stats["profile_inactive_or_non_equity"] += 1
            continue
        other_market = _venue(profile)
        if other_market and market and not (
            market == other_market or {market, other_market} <= {LSE_UNSPLIT, "AIM"}
            or {market, other_market} <= {LSE_UNSPLIT, "Main Market"}
        ):
            stats["conflicting_venue_profiles"] += 1
            profile = {}
            normalized = base
        elif market == LSE_UNSPLIT and other_market:
            market = other_market
        name = _text(base.get("companyName") or base.get("name") or profile.get("companyName") or symbol)
        raw_isin = _text(profile.get("isin")).upper()
        isin = raw_isin if ISIN.fullmatch(raw_isin) else ""
        if not isin:
            stats["symbols_without_matched_isin"] += 1
        item = FMPListing(_ticker(symbol), isin, name, market, _kind(normalized), SCREENER_DOC,
                          symbol, as_of=snapshot, status="Active in FMP",
                          venues=(market,))
        entries.append((item, normalized if isin else {}))

    # Group matches across FMP quote symbols by verified ISIN or legal-issuer
    # name. Invalid/missing ISIN never matches a different missing identifier.
    parents = list(range(len(entries)))
    index = {}

    def find(i):
        while i != parents[i]:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, (item, _) in enumerate(entries):
        keys = [("name", _name_key(item.company))]
        if item.isin:
            keys.append(("isin", item.isin))
        for key in keys:
            if key in index:
                parents[find(i)] = find(index[key])
            else:
                index[key] = i
    groups = defaultdict(list)
    for i, entry in enumerate(entries):
        groups[find(i)].append(entry)
    listings, profiles = [], {}
    for group in groups.values():
        listing, profile = min(group, key=_prefer)
        share_isins = {item.isin for item, _ in group if item.isin}
        kinds = {item.kind for item, _ in group}
        kind = "REIT" if "REIT" in kinds else "Fund" if "Fund" in kinds else listing.kind
        venues = {item.market for item, _ in group}
        if venues.intersection({"Main Market", "AIM"}):
            venues.discard(LSE_UNSPLIT)
        listing = replace(listing, kind=kind, venues=tuple(sorted(venues)))
        listings.append(listing)
        if profile:
            if len(share_isins) > 1:
                profile = dict(profile, uk_screen_multiple_isins=True)
            profiles[listing.issuer_key] = profile
    stats["duplicate_issuer_symbols_collapsed"] = len(entries) - len(listings)
    return sorted(listings, key=lambda item: (item.company.casefold(), item.issuer_key)), profiles


async def fetch_fmp_universe(client, *, now=None):
    """Return visible venue candidates and matching FMP profiles by issuer_key."""
    now = now or datetime.now(timezone.utc)
    stats = Counter()
    candidates = {}
    for exchange in EXCHANGE_FILTERS:
        try:
            rows = await _screen(client, exchange)
        except FMPError as exc:
            if (exchange == "LSE" or getattr(client, "_fatal", None)
                    or exc.status not in (400, 402, 403, 404, 410)):
                raise
            stats[f"{exchange} FMP query unavailable (HTTP {exc.status})"] = 1
            continue
        stats[f"{exchange} screener rows"] = len(rows)
        for row in rows:
            symbol, market = _canonical_symbol(row), _venue(row)
            if not symbol or not market or _non_equity(row) or boolean(row.get("isActivelyTrading")) is False:
                stats["unusable_screener_rows"] += 1
                continue
            existing = candidates.get(symbol)
            if existing is None or (_venue(existing) == LSE_UNSPLIT and market != LSE_UNSPLIT):
                candidates[symbol] = row
    if not candidates or not stats["LSE screener rows"]:
        raise FMPError("fmp_uk_screener_universe_empty")
    profile_rows = await _profiles(client, candidates, stats)
    if len(candidates) >= 50 and len(profile_rows) / len(candidates) < .4:
        raise FMPError("fmp_uk_screener_profile_coverage_below_40_percent")
    listings, profiles = _assemble(candidates, profile_rows, stats, now.date().isoformat())
    stats["unique_fmp_symbols"] = len(candidates)
    stats["unique_fmp_issuers"] = len(listings)
    stats["matched_fmp_profiles"] = len(profiles)
    client.uk_universe_stats = dict(stats)
    return listings, profiles
