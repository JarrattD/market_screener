"""Official UK equity listings, independently of FMP's issuer-country tags."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from openpyxl import load_workbook

LSE_DOCS = "https://docs.londonstockexchange.com"
LSE_INSTRUMENTS = f"{LSE_DOCS}/reports/instruments/latest.json"
LSE_FUNDS = f"{LSE_DOCS}/reports/listed-investment-funds/latest.json"
AQ_COMPANIES = "https://www.aquis.eu/companies"
MARKETS = {"MAIN MARKET": "Main Market", "MAIN MARKET - SFS": "Main Market", "AIM": "AIM"}
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
ELIGIBLE_COUNTRIES = {"GB", "GG", "JE"}
COUNTRY_ALIASES = {
    "UK": "GB", "GB": "GB", "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB",
    "ENGLAND": "GB", "SCOTLAND": "GB", "WALES": "GB", "NORTHERN IRELAND": "GB",
    "GUERNSEY": "GG", "GG": "GG", "JERSEY": "JE", "JE": "JE",
    "ISLE OF MAN": "IM", "IM": "IM", "UNITED STATES": "US", "CANADA": "CA",
    "IRELAND": "IE", "AUSTRALIA": "AU", "NEW ZEALAND": "NZ", "FRANCE": "FR",
    "GERMANY": "DE", "NETHERLANDS": "NL", "SWITZERLAND": "CH", "SINGAPORE": "SG",
    "HONG KONG": "HK", "CHINA": "CN", "ISRAEL": "IL", "CYPRUS": "CY",
    "BERMUDA": "BM", "CAYMAN ISLANDS": "KY", "BRITISH VIRGIN ISLANDS": "VG",
    "GIBRALTAR": "GI", "LUXEMBOURG": "LU", "SPAIN": "ES", "ITALY": "IT",
    "SOUTH AFRICA": "ZA", "SWEDEN": "SE", "NORWAY": "NO", "BELGIUM": "BE",
    "INDIA": "IN", "MALTA": "MT", "PORTUGAL": "PT", "JAPAN": "JP",
}
HQ_PLACES = {**{key: val for key, val in COUNTRY_ALIASES.items() if len(key) > 2},
             "LONDON": "GB", "EDINBURGH": "GB", "GLASGOW": "GB",
             "BELFAST": "GB", "CARDIFF": "GB", "MANCHESTER": "GB",
             "ST PETER PORT": "GG", "SAINT PETER PORT": "GG", "ST HELIER": "JE"}
HEADQUARTERED = re.compile(
    r"\b(?:headquartered|headquarters\s+(?:is|are)|head\s+office\s+(?:is|located))"
    r"\s+(?:located\s+)?(?:in|at)\s+([^;\n]{1,150})", re.IGNORECASE)


@dataclass(frozen=True)
class Listing:
    ticker: str
    isin: str
    company: str
    market: str
    kind: str
    source_url: str
    fmp_symbol: str
    incorporation: str = ""
    as_of: str = ""
    status: str = "Listed"
    venues: tuple[str, ...] = ()
    instrument: str = ""

    @property
    def issuer_key(self) -> str:
        return self.isin


@dataclass(frozen=True)
class HQDecision:
    status: str
    country: str
    evidence: str


def _clean(value) -> str:
    return str(value or "").strip()


def _country(value) -> str:
    name = str(value or "").strip().upper()
    return COUNTRY_ALIASES.get(name, name if re.fullmatch(r"[A-Z]{2}", name) else "")


def _guess_fmp_symbol(ticker: str, market: str) -> str:
    return f"{ticker}.L" if market != "Aquis" else ""


@dataclass(frozen=True)
class _RawResponse:
    content: bytes

    @property
    def text(self):
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.content)


def _request(session, url):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; equity-universe/1.0)",
               "Cache-Control": "no-cache"}
    if session is not None:
        response = session.get(url, timeout=(12, 65), headers=headers)
        response.raise_for_status()
        return response
    with urlopen(Request(url, headers=headers), timeout=65) as response:
        return _RawResponse(response.read())


def _xlsx_urls(session, metadata_url, keys):
    records = _request(session, metadata_url).json()["Data"]
    result = {}
    for key, prefix in keys.items():
        matches = [row for row in records if row["fileName"].casefold().startswith(prefix)
                   and row["fileName"].casefold().endswith(".xlsx")]
        if len(matches) != 1:
            raise ValueError(f"Expected one {prefix} workbook in {metadata_url}; got {len(matches)}")
        url = matches[0]["downloadLink"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "docs.londonstockexchange.com":
            raise ValueError("Unexpected LSE workbook host")
        result[key] = url
    return result


def _workbook_rows(blob, sheet_prefix):
    workbook = load_workbook(BytesIO(blob), read_only=True, data_only=True)
    try:
        matches = [name for name in workbook.sheetnames if name.startswith(sheet_prefix)]
        if len(matches) != 1:
            raise ValueError(f"Unexpected workbook tabs for {sheet_prefix}: {workbook.sheetnames}")
        return list(workbook[matches[0]].values)
    finally:
        workbook.close()


def _table(rows, required):
    for i, row in enumerate(rows[:20]):
        headers = [_clean(v).lower() for v in row]
        if required.issubset(headers):
            return [dict(zip(headers, data)) for data in rows[i + 1:]]
    raise ValueError(f"Official workbook no longer contains headers {sorted(required)}")


def _lse_report_date(rows):
    for row in rows[:8]:
        for cell in row:
            match = re.search(r"\bas at (\d{1,2} [A-Za-z]+ \d{4})", _clean(cell), re.I)
            if match:
                return datetime.strptime(match.group(1), "%d %B %Y").date()
    raise ValueError("Missing LSE report date")


def _check_date(source, snapshot, today, max_days):
    age_days = (today - snapshot).days
    if age_days < -1 or age_days > max_days:
        raise ValueError(f"{source} snapshot dated {snapshot} is not current for {today}")


def _fund_isins(rows):
    data = _table(rows, {"isin"})
    return {_clean(item.get("isin")).upper() for item in data
            if ISIN.fullmatch(_clean(item.get("isin")).upper())}


def _lse_list(rows, source, fund_isins, reit_isins, data_as_of):
    result = []
    for row in _table(rows, {"tidm", "issuer name", "isin", "lse market", "mifir identifier code"}):
        market = MARKETS.get(_clean(row.get("lse market")).upper())
        code = _clean(row.get("mifir identifier code")).upper()
        if not market or code not in {"SHRS", "DPRS", "OTHR"}:
            continue
        ticker, company = _clean(row.get("tidm")).upper(), _clean(row.get("issuer name"))
        isin, category = _clean(row.get("isin")).upper(), _clean(row.get("fca listing category")).lower()
        instrument = _clean(row.get("instrument name"))
        if not company or not ticker or not ISIN.fullmatch(isin):
            continue
        if isin in reit_isins or re.search(r"\bREIT\b", company, re.I):
            kind = "REIT"
        elif isin in fund_isins or "closed-ended investment fund" in category \
             or _clean(row.get("lse market")).upper().endswith(" - SFS"):
            kind = "Fund"
        else:
            kind = "Company"
        result.append(Listing(ticker, isin, company, market, kind, source,
                              _guess_fmp_symbol(ticker, market),
                              _clean(row.get("country of incorporation")), data_as_of,
                              instrument=instrument))
    return result


class _NextDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.fragments = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.inside = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.fragments.append(data)


def _parse_aquis(html, today):
    parser = _NextDataParser()
    parser.feed(html)
    if not parser.fragments:
        raise ValueError("Aquis Companies page no longer exposes its stock universe")
    rows = json.loads("".join(parser.fragments))["props"]["pageProps"]["companies"]
    dates = []
    for row in rows:
        for quote in row.get("recent_prices") or ():
            value = _clean(quote.get("datetime"))
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                dates.append(date.fromisoformat(value))
    if not dates:
        raise ValueError("Aquis universe has no dated quotes to check source freshness")
    snapshot = max(dates)
    _check_date("Aquis", snapshot, today, 14)
    objects = []
    for row in rows:
        ticker, isin = _clean(row.get("symbol")).upper(), _clean(row.get("isin")).upper()
        company, instrument, listing_type = (_clean(row.get(key)) for key in
                                              ("name", "instrument_type", "listing_type_name"))
        status = _clean(row.get("status")).upper()
        if listing_type.lower() not in {"access", "apex", "aram", "main", "main market"}:
            continue
        if status not in {"ACTIVE", "SUSPENDED"} or "shares" not in instrument.lower() \
                or not ticker or not company or not ISIN.fullmatch(isin):
            continue
        kind = ("REIT" if re.search(r"\breit\b", company, re.I) else
                "Fund" if re.search(r"\b(?:fund|investment trust|investment company)\b", company, re.I)
                else "Company")
        objects.append(Listing(ticker, isin, company, "Aquis", kind,
                               f"{AQ_COMPANIES}/{ticker}", "", "", snapshot.isoformat(),
                               status.title(), instrument=instrument))
    return objects


def _name_key(company):
    text = re.sub(r"\b(?:non[- ]voting shares|\d+(?:\.\d+)?% preference shares)\s*$",
                  "", company, flags=re.I).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w]+", "", text)
    return re.sub(r"(limited|ltd)$", "ltd", text)


def _preference(listing):
    instrument = listing.instrument.casefold()
    ordinary = bool(re.search(r"\bord(?:inary)?\b", instrument))
    secondary = bool(re.search(r"\b(?:non[- ]voting|pref(?:erence)?|144a)\b", instrument))
    # Prefer the core share over a second currency line, preference share, or DR.
    return (secondary, not ordinary, {"Main Market": 0, "AIM": 1, "Aquis": 2}[listing.market],
            listing.status == "Suspended", listing.ticker)


def _deduplicate(rows):
    parents = list(range(len(rows)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    index = {}
    for i, row in enumerate(rows):
        for key in (("isin", row.isin), ("name", _name_key(row.company))):
            if key in index:
                parents[find(i)] = find(index[key])
            else:
                index[key] = i
    parts = {}
    for i, row in enumerate(rows):
        parts.setdefault(find(i), []).append(row)
    results = []
    for part in parts.values():
        selected = min(part, key=_preference)
        kinds = {row.kind for row in part}
        kind = "REIT" if "REIT" in kinds else "Fund" if "Fund" in kinds else selected.kind
        results.append(replace(selected, kind=kind, venues=tuple(sorted({row.market for row in part}))))
    return sorted(results, key=lambda item: (item.company.casefold(), item.isin))


def load_universe(session=None, today=None) -> list[Listing]:
    """Load and validate the public official universe; no HQ or FMP filter applied."""
    today = today or date.today()
    instrument_url = _xlsx_urls(session, LSE_INSTRUMENTS, {"instruments": "instrument list"})["instruments"]
    fund_urls = _xlsx_urls(session, LSE_FUNDS,
                           {"funds": "listed investment funds list", "reit": "reits list"})
    equity = _workbook_rows(_request(session, instrument_url).content, "1.0 All Equity")
    snapshot = _lse_report_date(equity)
    _check_date("LSE instruments", snapshot, today, 55)
    flags = {}
    for kind, sheet in (("funds", "Listed Investment Funds List"), ("reit", "REITs")):
        rows = _workbook_rows(_request(session, fund_urls[kind]).content, sheet)
        flag_date = _lse_report_date(rows)
        _check_date(f"LSE {kind}", flag_date, today, 55)
        if abs((flag_date - snapshot).days) > 32:
            raise ValueError(f"LSE {kind} snapshot does not reconcile with equity file")
        flags[kind] = _fund_isins(rows)
    if len(flags["funds"]) < 150 or len(flags["reit"]) < 15:
        raise ValueError("Unexpectedly low LSE funds or REIT coverage")
    lse = _lse_list(equity, instrument_url, flags["funds"], flags["reit"], snapshot.isoformat())
    aq = _parse_aquis(_request(session, AQ_COMPANIES).text, today)
    if sum(row.market == "Main Market" for row in lse) < 700 or \
            sum(row.market == "AIM" for row in lse) < 450 or len(aq) < 50:
        raise ValueError("Unexpectedly low Main Market, AIM or Aquis source coverage")
    return _deduplicate(lse + aq)


def load_hq_overrides(path) -> dict:
    """Optional JSON keyed by ISIN: {country, source, note}. Requires real evidence."""
    path = Path(path)
    if not path.is_file():
        return {}
    overrides = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(overrides, dict):
        raise ValueError("HQ overrides must be a JSON object keyed by ISIN")
    for isin, record in overrides.items():
        if (not ISIN.fullmatch(isin) or not isinstance(record, dict)
                or not _country(record.get("country")) or not _clean(record.get("note"))
                or urlsplit(_clean(record.get("source"))).scheme not in {"http", "https"}
                or not urlsplit(_clean(record.get("source"))).netloc):
            raise ValueError(f"Invalid HQ override for {isin}")
    return overrides


def _description_country(description):
    matches = set()
    for match in HEADQUARTERED.finditer(description or ""):
        # Stop before the next sentence/statement where the format is unambiguous.
        fragment = re.split(r"\.\s+(?=[A-Z][a-z]{2,}\s)|,\s+(?=(?:and|with|which|it|the company)\b)",
                            match.group(1), maxsplit=1)[0]
        for place, country in sorted(HQ_PLACES.items(), key=lambda item: -len(item[0])):
            if place == "JERSEY" and re.search(r"\bNEW JERSEY\b", fragment, re.I):
                continue
            if re.search(r"(?<![A-Z])" + re.escape(place) + r"(?![A-Z])", fragment, re.I):
                matches.add(country)
    return matches


def _postcode_country(zip_code):
    zip_code = _clean(zip_code).upper()
    if re.match(r"^GY\d", zip_code):
        return "GG"
    if re.match(r"^JE\d", zip_code):
        return "JE"
    if re.match(r"^IM\d", zip_code):
        return "IM"
    if re.fullmatch(r"(?:GIR\s?0AA|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})", zip_code):
        return "GB"
    return ""


def classify_hq(listing: Listing, profile: Mapping | None, overrides: Mapping | None = None) -> HQDecision:
    """Do not confuse incorporation, exchange, or customer country with physical HQ."""
    if override := (overrides or {}).get(listing.isin):
        country = _country(override["country"])
        status = "in_scope" if country in ELIGIBLE_COUNTRIES else "out_of_scope"
        return HQDecision(status, country, f"Verified: {override['note']} ({override['source']})")
    if not profile:
        return HQDecision("review", "", "No usable matching FMP company profile")
    profile_isin = _clean(profile.get("isin")).upper()
    if profile_isin and profile_isin != listing.isin:
        return HQDecision("review", "", f"FMP profile ISIN {profile_isin} does not match official listing")
    stated = _country(profile.get("country"))
    postal = _postcode_country(profile.get("zip"))
    named = _description_country(_clean(profile.get("description")))
    city = _clean(profile.get("city"))
    address = _clean(profile.get("address"))
    places = ", ".join(part for part in (city, _clean(profile.get("zip"))) if part)
    if len(named) > 1 or (postal and named and postal not in named) or (postal and stated and postal != stated):
        return HQDecision("review", "", f"Conflicting FMP headquarters, country or postal evidence ({places})")
    if len(named) == 1:
        country = next(iter(named))
        if stated and country != stated:
            return HQDecision("review", country, f"FMP description says {country}, but profile country is {stated}")
        if country not in ELIGIBLE_COUNTRIES and not (stated == country and city and address):
            return HQDecision("review", country, "FMP description suggests an overseas head office; address unconfirmed")
        status = "in_scope" if country in ELIGIBLE_COUNTRIES else "out_of_scope"
        return HQDecision(status, country, f"FMP explicitly describes headquarters in {country}; {places}".rstrip("; "))
    if postal and postal in ELIGIBLE_COUNTRIES and city and address:
        return HQDecision("in_scope", postal,
                          f"Provisional FMP profile address ({places}); not explicitly labelled headquarters")
    return HQDecision("review", postal or stated, f"FMP has no clear headquarters evidence ({places or 'no address'})")
