"""FMP evidence for the official UK/Channel Islands equity universe.

Venue membership comes from the exchanges, never from the provider's country or
fund flags. Rows with coverage gaps survive with explicit missing inputs.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import re

from .book_margin_data import available_statements, latest_statement
from .data_freshness import DataFreshnessError, quote_timestamp, require_quote_current, require_statement_current
from .negative_ev_provider import FMPAuthenticationError, FMPError, _map_bounded, boolean, currency_units, number

UTC = timezone.utc
UK_CODES = {'LSE', 'AIM', 'AQSE', 'AQUIS', 'XLON', 'XLOM', 'AQEA', 'AQXA', 'AQXE'}


def _uk_profile(row, listed):
    code = str(row.get('exchange') or row.get('exchangeShortName') or '').upper()
    venue = str(row.get('exchangeFullName') or '').upper()
    symbol = str(row.get('symbol') or '').upper()
    return code in UK_CODES or 'LONDON STOCK EXCHANGE' in venue or 'AQUIS' in venue or (
        not code and not venue and symbol.endswith(('.L', '.AQ', '.AQSE')))


async def _get(client, endpoint, params=None, **kwargs):
    """Per-endpoint evidence failure, but propagate authentication/quota collapse."""
    try:
        return await client._request(endpoint, params, **kwargs), None
    except FMPAuthenticationError:
        raise
    except FMPError as exc:
        if getattr(client, '_fatal', None):
            raise client._fatal
        return [], exc


def _pick_profile(rows, listing):
    isin = str(listing.isin).upper()
    matches = [r for r in rows if str(r.get('isin') or '').upper() == isin
               and r.get('symbol') and _uk_profile(r, listing)]
    if not matches:
        return None
    expected = str(listing.fmp_symbol or '').upper()
    # Prefer the exact venue ticker; otherwise an unambiguous active UK symbol
    # for the very same class is acceptable (e.g., Aquis/LSE dual admission).
    active = [r for r in matches if boolean(r.get('isActivelyTrading')) is not False]
    matches = active or matches
    direct = [r for r in matches if r['symbol'].upper() == expected]
    matches = direct or matches
    if len({r['symbol'] for r in matches}) != 1:
        return None
    identity = ('currency', 'country', 'address', 'isActivelyTrading', 'isAdr')
    if any(len({str(r.get(k)) for r in matches if r.get(k) not in ('', None)}) > 1 for k in identity):
        return None
    return max(matches, key=lambda p: sum(v not in ('', None) for v in p.values()))


async def fetch_listing_profiles(client, listings):
    """Find FMP profiles by exchange ISIN; never attach a wrong-ticker company."""
    listings = {str(r.isin).upper(): r for r in listings if r.isin}
    wanted = set(listings)
    grouped = defaultdict(list)
    if getattr(client, 'use_profile_bulk', True) and wanted:
        for part in range(4):
            rows, error = await _get(client, '/stable/profile-bulk', {'part': part}, csv_response=True)
            if error:
                if error.status in (402, 403, 404, 410, 429):
                    break
            for row in rows:
                isin = str(row.get('isin') or '').upper()
                if isin in wanted and _uk_profile(row, listings[isin]):
                    grouped[isin].append(row)
    picked = {isin: x for isin, listing in listings.items()
              if (x := _pick_profile(grouped[isin], listing)) is not None}
    missing = sorted(wanted - picked.keys())
    async def one(isin):
        listing = listings[isin]
        symbol = str(listing.fmp_symbol or '')
        rows, direct_error = await _get(client, '/stable/profile', {'symbol': symbol}) if symbol else ([], None)
        picked_row = _pick_profile(rows, listing)
        if picked_row:
            picked[isin] = picked_row
            return
        if direct_error and direct_error.status in (401, 402, 403):
            return
        found, _ = await _get(client, '/stable/search-isin', {'isin': isin})
        # The lookup often returns foreign/OTC listings too; do not borrow them.
        variants = set(str(r.get('symbol') or '') for r in found if str(r.get('isin') or '').upper() == isin)
        variants = sorted(s for s in variants if s and s != symbol and s.upper().endswith(('.L', '.AQ', '.AQSE')))
        candidates = list(rows)
        for alt in variants:
            data, _ = await _get(client, '/stable/profile', {'symbol': alt})
            candidates.extend(data)
        if (match := _pick_profile(candidates, listing)) is not None:
            picked[isin] = match
    await _map_bounded(missing, one, client.concurrency)
    if wanted and not picked:
        raise FMPError('no_official_listings_matched_to_fmp_profiles')
    # Exception instead of silently publishing a mass-blank run on broken access.
    if len(wanted) >= 50 and len(picked) / len(wanted) < .25:
        raise FMPError('fmp_exact_isin_profile_coverage_below_25_percent')
    # Use exact exchange company labels as an extra share-class ambiguity signal.
    by_name = defaultdict(set)
    for isin, listing in listings.items():
        name = re.sub('[^a-z0-9]', '', str(listing.company or '').casefold())
        if name:
            by_name[name].add(isin)
    for name, isins in by_name.items():
        if len(isins) > 1:
            for isin in isins & picked.keys():
                picked[isin] = dict(picked[isin], uk_screen_multiple_isins=True)
    return picked


def _pick_quote(rows, symbol, now):
    candidates = [(quote_timestamp(x.get('timestamp')), x) for x in rows
                  if x.get('symbol') == symbol and number(x.get('marketCap')) is not None
                  and number(x.get('marketCap')) > 0]
    valid = [(stamp, x) for stamp, x in candidates if stamp is None or stamp <= now + timedelta(hours=1)]
    if not valid:
        return None, 'missing_usable_quote'
    dates = [stamp for stamp, _ in valid if stamp is not None]
    latest = max(dates) if dates else None
    recent = [r for stamp, r in valid if stamp == latest]
    currency_mismatch = len({currency_units(r['currency']) for r in recent if r.get('currency')}) > 1
    if currency_mismatch:
        return None, 'conflicting_quote_currencies'
    # A disagreement between usable correctly matched quotes is a screening warning.
    chosen = min(recent, key=lambda r: (
        number(r.get('price')) is None, number(r['marketCap']), number(r.get('price')) or 0))
    if len({(number(r.get('price')), number(r['marketCap'])) for r in recent}) != 1:
        return chosen, 'warning_conflicting_numeric_quotes_preliminary'
    if latest is None:
        return chosen, 'warning_quote_timestamp_missing'
    try:
        require_quote_current({'as_of': latest.isoformat()}, now)
    except DataFreshnessError:
        return chosen, 'warning_stale_quote_preliminary'
    return chosen, None


def _pick_shares(rows, symbol, now):
    matched = []
    for r in rows:
        stamp = quote_timestamp(r.get('date'))
        shares = number(r.get('outstandingShares'))
        source = str(r.get('source') or '')
        if not (r.get('symbol') == symbol and shares and shares > 0 and stamp
                and now - timedelta(days=45) <= stamp <= now + timedelta(hours=1)
                and source.startswith(('https://', 'http://'))):
            continue
        matched.append((stamp, r))
    if not matched:
        return None
    latest = max(t for t, _ in matched)
    rows = [r for t, r in matched if t == latest]
    return rows[0] if len({number(r['outstandingShares']) for r in rows}) == 1 else None


def reconcile_cap(profile, q, shares):
    """Apply screening-warning-v1, distinguishing units/identity from doubt."""
    if not q:
        return None, None, 'market_cap_quote_missing'
    if (boolean(profile.get('isAdr')) is True or profile.get('uk_screen_multiple_isins')):
        return None, None, 'whole_issuer_cap_needs_share_class_review'
    profile_unit = str(profile.get('currency') or '')
    quote_unit = str(q.get('currency') or '')
    base, scale = currency_units(profile_unit or quote_unit)
    if not re.fullmatch('[A-Z]{3}', base) or (profile_unit and quote_unit
                                                   and currency_units(quote_unit) != (base, scale)):
        return None, None, 'quote_currency_missing_or_conflicting'
    price, cap = number(q.get('price')), number(q.get('marketCap'))
    count = number((shares or {}).get('outstandingShares'))
    if cap is None or cap <= 0:
        return None, None, 'market_cap_missing_or_invalid'
    product = price * count * scale if price is not None and price > 0 and count and count > 0 else None
    if product is not None:
        possibilities = sorted(set(cap * k for k in (1.0, scale) if abs(product / (cap * k) - 1) <= .01))
        if len(possibilities) == 1:
            return possibilities[0], base, None
        # It is common to see moderate share-count discrepancies from timing.
        # A near x100 relationship is instead evidence that the cap is in the
        # same subunit as the price even where the count misses by over 1%.
        if scale < 1 and 0.75 <= product / (cap * scale) <= 1.25:
            return cap * scale, base, 'warning_price_x_shares_difference_subunit_cap_preliminary'
        warning = 'warning_market_cap_vs_price_times_shares_difference'
    else:
        warning = 'warning_independent_price_or_outstanding_shares_not_available'
    if scale != 1:
        warning += '_fmp_cap_assumed_whole_' + base
    return cap, base, warning


def _balance_report(rows, symbol, now):
    """Do not erase all book inputs on differing figures for the same period."""
    vetted = [r for r in available_statements(rows, symbol, now)
              if str(r.get('period') or '').upper() in {'FY', 'ANNUAL', 'Q1', 'Q2', 'Q3', 'Q4', 'H1', 'H2'}]
    if not vetted:
        return None, []
    end = max(str(r['date'])[:10] for r in vetted)
    same_day = [r for r in vetted if str(r['date'])[:10] == end]
    if len({currency_units(r['reportedCurrency']) for r in same_day if r.get('reportedCurrency')}) > 1:
        reviewed = latest_statement(vetted, symbol, as_of=now)
        # A clear newer revision can resolve a changed reporting currency;
        # without that clarity, do not blend or assume reporting units.
        return (reviewed, []) if reviewed and not reviewed.get('_diagnostic_conflicts') else (
            None, ['balance_currency_conflict'])
    reviewed = latest_statement(vetted, symbol, as_of=now)
    conflicts = (reviewed or {}).get('_diagnostic_conflicts')
    if reviewed and not conflicts:
        return reviewed, []
    numeric_keys = ('totalCurrentAssets', 'totalLiabilities', 'totalStockholdersEquity',
                    'totalEquity', 'minorityInterest', 'preferredStock', 'totalDebt',
                    'cashAndShortTermInvestments', 'goodwillAndIntangibleAssets')
    def quality(row):
        dated = quote_timestamp(row.get('acceptedDate') or row.get('filingDate') or row.get('fillingDate'))
        return (dated or datetime.min.replace(tzinfo=UTC),
                str(row['period']).upper() in ('FY', 'ANNUAL'),
                sum(number(row.get(k)) is not None for k in numeric_keys),
                tuple(str(row.get(k)) for k in numeric_keys))
    return max(same_day, key=quality), ['warning_balance_numeric_revisions_disagree_preliminary']


def _annual_periods(rows, symbol, now):
    fy = [r for r in available_statements(rows, symbol, now)
          if str(r.get('period') or '').upper() in ('FY', 'ANNUAL')]
    groups = defaultdict(list)
    for r in fy:
        groups[str(r['date'])[:10]].append(r)
    out, flags = [], []
    for end in sorted(groups, reverse=True)[:5]:
        same = groups[end]
        dated = [(quote_timestamp(r.get('acceptedDate') or r.get('filingDate') or r.get('fillingDate')), r) for r in same]
        signature = lambda r: (str(r.get('reportedCurrency') or '').upper(),
                               *(number(r.get(k)) for k in ('operatingCashFlow','capitalExpenditure','freeCashFlow')))
        if all(t for t, _ in dated):
            latest = max(t for t, _ in dated)
            same = [r for t, r in dated if t == latest]
        if len({signature(r) for r in same}) != 1:
            if len({currency_units(r['reportedCurrency']) for r in same if r.get('reportedCurrency')}) > 1:
                out.append({'date': end})
                flags.append('cashflow_conflicting_fy_currency:' + end)
                continue
            flags.append('warning_cashflow_numeric_revisions_disagree_preliminary:' + end)
        out.append(max(same, key=lambda row: (
            bool(row.get('reportedCurrency')),
            all(number(row.get(k)) is not None for k in ('operatingCashFlow', 'capitalExpenditure')),
            number(row.get('freeCashFlow')) is not None,
            tuple(str(v) for v in signature(row)),
        )))
    return out, flags


def _first(row, *keys):
    for key in keys:
        if (result := number(row.get(key))) is not None:
            return result
    return None


def _fcf(row):
    ocf, capex = number(row.get('operatingCashFlow')), number(row.get('capitalExpenditure'))
    if ocf is not None and capex is not None:
        return ocf - abs(capex), 'OCF less absolute capital expenditure'
    value = number(row.get('freeCashFlow'))
    return value, 'Reported FCF fallback' if value is not None else ''


def _score_inputs(cap, cap_ccy, balance, annual, rates, now, notes):
    # Monetary score inputs are in GBP. Inconsistent annual reporting currencies
    # are never assumed comparable even though spot FX exists for both.
    def to_gbp(value, currency):
        return value * rates[currency] if value is not None and currency in rates else None
    bc = str(balance.get('reportedCurrency') or '') if balance else ''
    if balance and bc not in rates:
        notes.append('balance_reporting_currency_or_fx_unavailable')
    if cap is not None and cap_ccy not in rates:
        notes.append('market_cap_currency_fx_unavailable')
    b = balance or {}
    parent_eq = number(b.get('totalStockholdersEquity'))
    equity, inc_nci = (parent_eq, False) if parent_eq is not None else (number(b.get('totalEquity')), True)
    combined_intangibles = number(b.get('goodwillAndIntangibleAssets'))
    if combined_intangibles is None:
        goodwill, intangible = number(b.get('goodwill')), number(b.get('intangibleAssets'))
        if goodwill is not None and intangible is not None:
            combined_intangibles = goodwill + intangible
    combined_cash = number(b.get('cashAndShortTermInvestments'))
    if combined_cash is None:
        cash, inv = number(b.get('cashAndCashEquivalents')), number(b.get('shortTermInvestments'))
        if cash is not None and inv is not None:
            combined_cash = cash + inv
    values, fcf_basis = [], []
    previous, contiguous = None, True
    anchor = str(annual[0].get('reportedCurrency') or '') if annual else ''
    for row in annual:
        period = date.fromisoformat(str(row['date'])[:10])
        if previous is not None and not 330 <= (previous - period).days <= 400:
            contiguous = False
            notes.append('non_comparable_fiscal_year_interval')
        previous = period
        currency = str(row.get('reportedCurrency') or '')
        fcf, basis = _fcf(row)
        fcf_basis.append(basis)
        if not contiguous or currency != anchor:
            values.append(None)
            if currency != anchor:
                notes.append('cashflow_reporting_currency_changed_or_unknown')
        else:
            values.append(to_gbp(fcf, currency))
    if not annual or not values or values[0] is None:
        notes.append('latest_fy_fcf_or_fx_unavailable')
    if len(values) != 5 or any(v is None for v in values):
        notes.append('five_comparable_fy_fcf_values_unavailable')
    return dict(
        market_cap=to_gbp(cap, cap_ccy),
        current_assets=to_gbp(number(b.get('totalCurrentAssets')), bc),
        total_liabilities=to_gbp(number(b.get('totalLiabilities')), bc),
        minority_interest=to_gbp(_first(b, 'minorityInterest', 'noncontrollingInterest'), bc),
        preferred_equity=to_gbp(number(b.get('preferredStock')), bc),
        total_equity=to_gbp(equity, bc),
        equity_includes_minority=inc_nci,
        goodwill_and_intangibles=to_gbp(combined_intangibles, bc),
        total_debt=to_gbp(number(b.get('totalDebt')), bc),
        cash_and_short_term_investments=to_gbp(combined_cash, bc),
        annual_fcf=values,
        ownership_pct=None,
    ), fcf_basis


async def fetch_financials(client, profiles, *, now=None):
    """Return scored-input and auditable native evidence for each supplied ISIN."""
    now = now or datetime.now(UTC)
    symbols = sorted({r['symbol'] for r in profiles.values() if r.get('symbol')
                      and boolean(r.get('isActivelyTrading')) is not False})
    quotes = {}
    missing = []
    for start in range(0, len(symbols), 100):
        batch = symbols[start:start + 100]
        rows, err = await _get(client, '/stable/batch-quote', {'symbols': ','.join(batch)})
        if err and err.status in (402, 403):
            for sym in symbols[start:]:
                quotes[sym] = (None, 'batch_quote_access_denied')
            break
        for sym in batch:
            picked, reason = _pick_quote(rows, sym, now)
            if picked is not None or reason != 'missing_usable_quote':
                quotes[sym] = (picked, reason)
            else:
                missing.append(sym)
    async def get_quote(sym):
        rows, err = await _get(client, '/stable/quote', {'symbol': sym})
        quotes[sym] = _pick_quote(rows, sym, now) if not err else (None, 'quote:' + err.reason)
    await _map_bounded(missing, get_quote, client.concurrency)

    async def get_statements(sym, endpoint, period, limit):
        return await _get(client, '/stable/' + endpoint, {'symbol': sym, 'period': period, 'limit': limit})
    bundles = {}
    async def one(sym):
        shares, ba, bq, cf = await asyncio.gather(
            _get(client, '/stable/shares-float', {'symbol': sym}),
            get_statements(sym, 'balance-sheet-statement', 'annual', 3),
            get_statements(sym, 'balance-sheet-statement', 'quarter', 4),
            get_statements(sym, 'cash-flow-statement', 'annual', 10))
        notes = [stage + ':' + pair[1].reason for stage, pair in
                 zip(('shares', 'balance_annual', 'balance_interim', 'cashflow_annual'), (shares, ba, bq, cf)) if pair[1]]
        # If this account is denied across an entire dataset, fail the workflow
        # rather than erase an existing ranking with blanks for everyone.
        share = _pick_shares(shares[0], sym, now)
        balance, balance_notes = _balance_report(ba[0] + bq[0], sym, now)
        notes.extend(balance_notes)
        if balance:
            try:
                require_statement_current(balance, 'Balance', now.date())
            except DataFreshnessError:
                notes.append('warning_stale_balance_preliminary')
        else:
            notes.append('missing_or_conflicting_balance')
        annual, fcf_notes = _annual_periods(cf[0], sym, now)
        notes.extend(fcf_notes)
        if annual:
            try:
                require_statement_current(annual[0], 'Latest annual cashflow', now.date())
            except DataFreshnessError:
                notes.append('warning_stale_annual_cashflow_preliminary')
        bundles[sym] = (share, balance, annual, notes)
    await _map_bounded(symbols, one, client.concurrency)
    if symbols and not any(b[1] is not None or b[2] for b in bundles.values()):
        raise FMPError('no_fmp_statements_available_for_uk_screen')

    currencies = {c for sh, b, a, n in bundles.values()
                  for r in ([b] if b else []) + a if (c := str(r.get('reportedCurrency') or ''))}
    currencies.update(c for p in profiles.values() if (c := currency_units(p.get('currency'))[0]))
    non_gbp = {x for x in currencies if currency_units(x)[0] != 'GBP'}
    fx, _ = await client.fetch_fx(non_gbp) if non_gbp else ({}, [])
    rates = {k: v.rate_to_gbp for k, v in fx.items()}
    rates.update({k: currency_units(k)[1] for k in currencies if currency_units(k)[0] == 'GBP'})
    evidence = {}
    for isin, p in profiles.items():
        sym = p.get('symbol') or ''
        if boolean(p.get('isActivelyTrading')) is False:
            evidence[isin] = dict(symbol=sym, currency='GBP', status='Inactive',
                                  score_inputs={'market_status': 'Inactive'},
                                  notes=['fmp_marks_security_inactive'])
            continue
        sh, b, a, ns = bundles.get(sym, (None, None, [], ['no_matching_fmp_symbol']))
        ns = list(ns)
        q, qr = quotes.get(sym, (None, 'missing_quote'))
        if qr:
            ns.append(qr)
            if q and q.get('timestamp'):
                stamp = quote_timestamp(q['timestamp'])
                if stamp:
                    ns.append('FMP quote timestamp ' + stamp.isoformat(timespec='seconds'))
        # Correctly matched profile cap is a preliminary fallback, with visible
        # provenance, when the market feed does not have a usable quote.
        if not q and qr != 'conflicting_quote_currencies':
            profile_cap = number(p.get('marketCap') or p.get('mktCap'))
            if profile_cap is not None and profile_cap > 0:
                q = {**{k: p.get(k) for k in ('symbol', 'price', 'currency', 'timestamp')},
                     'marketCap': profile_cap, 'source': 'FMP exact-ISIN company profile'}
                ns.append('warning_matched_fmp_profile_cap_used_no_usable_quote')
        cap, cc, cr = reconcile_cap(p, q, sh)
        if cr:
            ns.append(cr)
        if cap is not None and q and sh and cr and cr.startswith('warning_'):
            price, count = number(q.get('price')), number(sh.get('outstandingShares'))
            if price is not None and count and count > 0:
                px_ccy, px_unit = currency_units(p.get('currency') or q.get('currency'))
                product = price * count * px_unit
                ns.append(f'FMP cap {cap:.6g} {cc} vs quote × shares {product:.6g} {px_ccy} '
                          f'({(product / cap - 1) * 100:+.2f}%; share record {sh.get("date")})')
        inputs, fcf_basis = _score_inputs(cap, cc, b, a, rates, now, ns)
        status = 'Check' if (cr and not cr.startswith('warning_') and ('review' in cr or 'conflict' in cr)) or qr == 'conflicting_quote_currencies' else (
            'Partial' if ns else 'Available')
        if status == 'Check':
            inputs['market_status'] = status
        evidence[isin] = dict(symbol=sym, currency='GBP', status=status, score_inputs=inputs,
                              notes=list(dict.fromkeys(ns)), quote=q, shares=sh,
                              balance=b, annual_cashflows=a, annual_fcf_basis=fcf_basis,
                              foreign_exchange={c: f.to_dict() for c, f in fx.items()
                                if c in {cc, (b or {}).get('reportedCurrency'), *(r.get('reportedCurrency') for r in a)}})
    return evidence
