"""Pure, conservative calculations for a negative-EV / five-year-FCF screen.

No API, filesystem or floating-point calculation is performed here. Monetary
results are Decimal objects: serialize them as strings at an IO boundary.

A valid observation has status='ok' and a Boolean ``passes``/``passed``. Data
that cannot establish the answer is MissingData (a dict, not an exception),
with status='missing' and passes=None. In particular, missing is never False.

Input statements accept FMP names (date, reportedCurrency, freeCashFlow,
period, fiscalYear/calendarYear, acceptedDate/filingDate). Normalized statements
can additionally supply period_start/startDate, period_end/endDate, and
basis='quarter'|'ytd'|'annual'|'ttm'. Interval starts are INCLUSIVE. Unlabelled
Q2/Q3 cash flows are NOT assumed to be discrete. Inference requires complete
annual reconciliation or an independent reported TTM cross-check.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import product
from typing import Any, Iterable, Mapping

ZERO = Decimal('0')
ONE = Decimal('1')
DAY = timedelta(days=1)
# API numbers can be rounded by one reporting-currency unit. No relative
# tolerance is used: material disagreements cannot disappear for large firms.
RECONCILIATION_TOLERANCE = Decimal('1')
OPTIONAL_ANNUAL_AMOUNT_ERRORS = frozenset({
    'missing_fcf', 'missing_numeric_value', 'invalid_numeric_value', 'ambiguous_capex_sign',
})


class MissingData(dict):
    """An indeterminate screen observation, deliberately distinct from a fail."""
    def __init__(self, reason: str, detail: str = '', **context: Any):
        super().__init__(status='missing', passes=None, passed=None,
                         reason_codes=[reason], reason=reason, detail=detail,
                         **context)


class _Invalid(ValueError):
    def __init__(self, reason: str, detail: str):
        self.reason, self.detail = reason, detail
        super().__init__(detail)


def _number(value: Any, field: str) -> Decimal:
    if value is None or value == '' or isinstance(value, bool):
        raise _Invalid('missing_numeric_value', f'{field} is missing or invalid')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise _Invalid('invalid_numeric_value', f'{field} is not a decimal')
    if not result.is_finite():
        raise _Invalid('invalid_numeric_value', f'{field} must be finite')
    return result


def _currency(value: Any, field: str = 'reportedCurrency') -> str:
    if not isinstance(value, str) or len(value.strip()) != 3 or not value.strip().isalpha():
        raise _Invalid('missing_or_invalid_currency', f'{field} must be a three-letter currency')
    # Do not uppercase pence/cent quotation codes: GBp is not GBP.
    if value.strip() != value.strip().upper() or value.strip() in {'GBX', 'ZAC', 'ILA'}:
        raise _Invalid('unsupported_currency_unit', f'{field} must identify whole currency units, not {value!r}')
    return value.strip()


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) is not None and row.get(key) != '':
            return row[key]
    return None


def _date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        raise _Invalid('missing_or_invalid_date', f'{field} is missing or invalid')


def _shift_year(value: date, count: int) -> date:
    year = value.year + count
    return date(year, value.month, min(value.day, monthrange(year, value.month)[1]))


def _month_end(value: date) -> bool:
    return value.day == monthrange(value.year, value.month)[1]


def _shift_month_end(value: date, count: int) -> date:
    index = value.year * 12 + value.month - 1 + count
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, monthrange(year, month0 + 1)[1])


def _close(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= RECONCILIATION_TOLERANCE


def _fx_rate(fx: Any, source: str, target: str) -> Decimal:
    if source == target:
        return ONE
    value = None
    if isinstance(fx, Mapping):
        if 'rate' in fx:
            if fx.get('from_currency') != source or fx.get('to_currency') != target:
                raise _Invalid('fx_currency_mismatch', 'FX quote direction does not match market cap -> reporting currency')
            value = fx['rate']
        else:
            value = fx.get((source, target), fx.get(f'{source}/{target}'))
    if value is None:
        raise _Invalid('missing_fx', f'Need a labelled {source} -> {target} FX rate')
    result = _number(value, 'FX rate')
    if result <= ZERO:
        raise _Invalid('invalid_fx', 'FX rate must be positive')
    return result


def enterprise_value(cap: Any, cap_currency: str,
                     balance: Mapping[str, Any], fx: Any = None) -> dict:
    """EV = converted market cap + totalDebt - cashAndShortTermInvestments.

    A combined cash field is used ONCE; if absent both cashAndCashEquivalents
    and shortTermInvestments must be explicitly supplied. totalDebt is used
    once, with no extra leases. FX is a labelled directional quote, e.g.
    {'from_currency':'USD','to_currency':'EUR','rate':'0.92'}, or a mapping
    {('USD','EUR'):'0.92'}. Unlabelled scalars are intentionally rejected.
    """
    try:
        if not isinstance(balance, Mapping):
            raise _Invalid('missing_balance', 'A balance-sheet statement is required')
        source = _currency(cap_currency, 'market-cap currency')
        target = _currency(_first(balance, 'reportedCurrency', 'currency'))
        market_cap = _number(cap, 'market capitalization')
        if market_cap <= ZERO:
            raise _Invalid('invalid_market_cap', 'Market capitalization must be positive and verified')
        debt = _number(balance.get('totalDebt'), 'totalDebt')
        cash_field = _first(balance, 'cashAndShortTermInvestments')
        if cash_field is not None:
            cash = _number(cash_field, 'cashAndShortTermInvestments')
            cash_source = 'cashAndShortTermInvestments'
        else:
            cash_equivalents = _number(balance.get('cashAndCashEquivalents'), 'cashAndCashEquivalents')
            investments = _number(balance.get('shortTermInvestments'), 'shortTermInvestments')
            if cash_equivalents < ZERO or investments < ZERO:
                raise _Invalid('negative_balance_component', 'Cash and short-term investments must each be non-negative')
            cash = cash_equivalents + investments
            cash_source = 'cashAndCashEquivalents + shortTermInvestments'
        if market_cap < ZERO or debt < ZERO or cash < ZERO:
            raise _Invalid('negative_balance_component', 'Market cap, debt, and cash must be non-negative')
        rate = _fx_rate(fx, source, target)
        converted = market_cap * rate
        ev = converted + debt - cash
        return dict(status='ok', enterprise_value=ev, ev=ev, currency=target,
                    reporting_currency=target, market_cap=converted,
                    market_cap_original=market_cap, market_cap_currency=source,
                    fx_rate=rate, total_debt=debt, cash_and_short_term_investments=cash,
                    cash_source=cash_source, passes=ev < ZERO, passed=ev < ZERO,
                    reason_codes=[], warnings=[])
    except _Invalid as error:
        return MissingData(error.reason, error.detail)


@dataclass(frozen=True)
class _Row:
    start: date | None
    end: date
    amount: Decimal
    currency: str
    period: str
    fiscal_year: int | None
    basis: str
    revision: str | None
    raw: Mapping[str, Any]


def _amount(row: Mapping[str, Any], ttm: bool) -> Decimal:
    direct = _first(row, 'freeCashFlowTTM', 'freeCashFlow', 'fcf') if ttm else _first(row, 'freeCashFlow', 'fcf')
    if direct is not None:
        return _number(direct, 'free cash flow')
    operating = _first(row, 'operatingCashFlow', 'netCashProvidedByOperatingActivities')
    capex = row.get('capitalExpenditure')
    if operating is None or capex is None:
        raise _Invalid('missing_fcf', 'Free cash flow or both operating cash flow and signed capital expenditure are required')
    op, investment = _number(operating, 'operating cash flow'), _number(capex, 'capital expenditure')
    if investment > ZERO:
        raise _Invalid('ambiguous_capex_sign', 'Positive capital expenditure has an unverified sign convention')
    return op + investment


def _revision(value: Any) -> str:
    try:
        timestamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        raise _Invalid('invalid_revision_date', 'Filing / acceptance timestamp is invalid')


def _latest_raw_revisions(rows: Iterable[Mapping[str, Any]] | None, kind: str,
                          as_of: date) -> list[Mapping[str, Any]]:
    groups: dict[tuple[date, str], list[Mapping[str, Any]]] = {}
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            raise _Invalid('invalid_statement', f'{kind} statement is not an object')
        end = _date(_first(raw, 'period_end', 'endDate', 'date'), 'statement end')
        revision = _first(raw, 'acceptedDate', 'filingDate', 'fillingDate')
        if end > as_of or (revision and _date(revision, 'filing date') > as_of):
            continue
        period = str(raw.get('period') or ('FY' if kind == 'annual' else 'TTM' if kind == 'ttm' else '')).upper()
        groups.setdefault((end, period), []).append(raw)
    output = []
    for versions in groups.values():
        revisions = [_first(row, 'acceptedDate', 'filingDate', 'fillingDate') for row in versions]
        if len(versions) > 1 and all(revisions):
            keys = [_revision(revision) for revision in revisions]
            latest = max(keys)
            output.extend(row for row, key in zip(versions, keys) if key == latest)
        else:
            output.extend(versions)
    return output


def _rows(rows: Iterable[Mapping[str, Any]] | None, kind: str,
          as_of: date, symbol: str | None) -> list[_Row]:
    parsed: list[_Row] = []
    for raw in _latest_raw_revisions(rows, kind, as_of):
        if not isinstance(raw, Mapping):
            raise _Invalid('invalid_statement', f'{kind} statement is not an object')
        if symbol and raw.get('symbol') and raw['symbol'] != symbol:
            raise _Invalid('symbol_mismatch', f'Unexpected statement symbol {raw["symbol"]}')
        end = _date(_first(raw, 'period_end', 'endDate', 'date'), 'statement end')
        revision_raw = _first(raw, 'acceptedDate', 'filingDate', 'fillingDate')
        if end > as_of or (revision_raw and _date(revision_raw, 'filing date') > as_of):
            continue
        start_raw = _first(raw, 'period_start', 'startDate')
        start = _date(start_raw, 'statement start') if start_raw else None
        if start and start > end:
            raise _Invalid('invalid_period', 'Statement starts after it ends')
        period = str(raw.get('period') or ('FY' if kind == 'annual' else 'TTM' if kind == 'ttm' else '')).upper()
        basis = str(raw.get('basis') or raw.get('periodType') or ('unknown' if kind == 'interim' else kind)).lower()
        basis = {'discrete': 'quarter', 'quarterly': 'quarter', 'cumulative': 'ytd', 'year-to-date': 'ytd', 'fy': 'annual'}.get(basis, basis)
        if kind == 'annual' and period not in ('FY', 'ANNUAL'):
            raise _Invalid('non_annual_statement', 'Annual input includes a non-full-year statement')
        if kind == 'annual' and start and not 350 <= (end - start).days + 1 <= 378:
            raise _Invalid('non_full_fiscal_year', f'Annual period {start} to {end} is a stub or irregular year')
        year_raw = _first(raw, 'fiscalYear', 'calendarYear', 'fiscal_year')
        try:
            year = int(year_raw) if year_raw is not None else None
        except (ValueError, TypeError):
            raise _Invalid('invalid_fiscal_year', 'Fiscal year must be an integer')
        parsed.append(_Row(start, end, _amount(raw, kind == 'ttm'),
                           _currency(_first(raw, 'reportedCurrency', 'currency')),
                           period, year, basis, _revision(revision_raw) if revision_raw else None, raw))
    # Rows for one reporting endpoint are revisions, NOT extra periods.
    groups: dict[tuple[date, str], list[_Row]] = {}
    for row in parsed:
        groups.setdefault((row.end, row.period), []).append(row)
    output = []
    for key, versions in groups.items():
        signatures = {(r.start, r.amount, r.currency, r.fiscal_year, r.basis) for r in versions}
        if len(signatures) == 1:
            output.append(max(versions, key=lambda r: r.revision or ''))
            continue
        if all(r.revision for r in versions):
            latest_revision = max(r.revision for r in versions)
            latest = [r for r in versions if r.revision == latest_revision]
            if len({(r.start, r.amount, r.currency, r.fiscal_year, r.basis) for r in latest}) == 1:
                output.append(latest[0])
                continue
        raise _Invalid('ambiguous_restatement', f'Conflicting {kind} statements ending {key[0]} lack an unambiguous latest filing')
    return sorted(output, key=lambda r: r.end)


def _optional_rows(rows: list[Mapping[str, Any]], kind: str, cutoff: date,
                   symbol: str | None, warnings: list[str]) -> list[_Row]:
    """Isolate endpoint errors instead of discarding unrelated usable data.

    All versions of one endpoint are validated together. A bad newest revision
    therefore cannot silently be replaced by an older valid version. Missing
    prefix quarters naturally prevent reconstruction, but a standalone explicit
    YTD does not depend on irrelevant quarters in the same or another year.
    """
    groups: dict[date, list[Mapping[str, Any]]] = {}
    for raw in rows:
        try:
            if not isinstance(raw, Mapping):
                raise _Invalid('invalid_statement', 'Statement is not an object')
            endpoint = _date(_first(raw, 'period_end', 'endDate', 'date'), 'statement end')
            filing = _first(raw, 'acceptedDate', 'filingDate', 'fillingDate')
            if endpoint > cutoff or (filing and _date(filing, 'filing date') > cutoff):
                continue
            groups.setdefault(endpoint, []).append(raw)
        except _Invalid as error:
            if kind == 'annual' and error.reason not in OPTIONAL_ANNUAL_AMOUNT_ERRORS:
                raise
            warnings.append(f'{kind.title()} observation unavailable: {error.reason}: {error.detail}')
    usable = []
    for endpoint, versions in groups.items():
        try:
            usable.extend(_rows(versions, kind, cutoff, symbol))
        except _Invalid as error:
            if kind == 'annual' and error.reason not in OPTIONAL_ANNUAL_AMOUNT_ERRORS:
                raise
            warnings.append(f'{kind.title()} data ending {endpoint} unavailable: {error.reason}: {error.detail}')
    return sorted(usable, key=lambda row: row.end)


def _selected_annual_rows(rows: list[Mapping[str, Any]], cutoff: date, symbol: str | None,
                          warnings: list[str]) -> tuple[list[_Row], date | None, _Invalid | None]:
    # Select endpoints BEFORE parsing amounts. An old null FCF is irrelevant,
    # but a null inside the newest five endpoint slots must never be dropped
    # and backfilled with an older positive year. A sixth endpoint is used only
    # to establish the first required year's start date.
    dated = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise _Invalid('invalid_statement', 'Annual statement is not an object')
        end = _date(_first(raw, 'period_end', 'endDate', 'date'), 'annual end')
        filing = _first(raw, 'acceptedDate', 'filingDate', 'fillingDate')
        if end <= cutoff and (not filing or _date(filing, 'filing date') <= cutoff):
            dated.append((end, raw))
    endpoints = sorted({end for end, _ in dated})
    if not endpoints:
        return [], None, None
    first_required = endpoints[-5] if len(endpoints) >= 5 else endpoints[0]
    predecessor = endpoints[-6] if len(endpoints) >= 6 else None
    selected = [raw for end, raw in dated if end >= first_required]
    try:
        return _rows(selected, 'annual', cutoff, symbol), predecessor, None
    except _Invalid as error:
        # Missing amounts can leave the annual route unavailable without
        # invalidating independent, explicitly bounded quarterly coverage.
        # Conflicting revisions, invalid intervals, and symbol inconsistencies
        # remain hard errors, not permission to ignore contradictory evidence.
        if error.reason not in OPTIONAL_ANNUAL_AMOUNT_ERRORS:
            raise
        usable = _optional_rows(selected, 'annual', cutoff, symbol, warnings)
        warnings.append('Annual comparison unavailable for one or more required fiscal years; independent quarterly coverage must establish the complete newest window')
        return usable, predecessor, error


def _annual_starts(annual: list[_Row], predecessor: date | None = None) -> dict[date, date | None]:
    result = {}
    for index, row in enumerate(annual):
        if row.start:
            start = row.start
        elif index and 350 <= (row.end - annual[index - 1].end).days <= 378:
            start = annual[index - 1].end + DAY
        elif index == 0 and predecessor and 350 <= (row.end - predecessor).days <= 378:
            start = predecessor + DAY
        else:
            start = None
        result[row.end] = start
    return result


def _five_annual(annual: list[_Row], starts: dict[date, date | None]) -> tuple[list[_Row], str]:
    if len(annual) < 5:
        raise _Invalid('insufficient_annual_history', 'Fewer than five complete annual cash-flow statements')
    chosen = annual[-5:]
    currency = chosen[0].currency
    for index, row in enumerate(chosen):
        if row.currency != currency:
            raise _Invalid('fcf_currency_mismatch', 'Selected five fiscal years use different currencies')
        if index:
            previous = chosen[index - 1]
            if not 350 <= (row.end - previous.end).days <= 378:
                raise _Invalid('annual_gap_or_irregular_period', 'Annual statement ends are not five consecutive full fiscal years')
            if row.fiscal_year is not None and previous.fiscal_year is not None and row.fiscal_year != previous.fiscal_year + 1:
                raise _Invalid('annual_fiscal_year_gap', 'Fiscal year identifiers are not consecutive')
            if starts[row.end] and starts[row.end] != previous.end + DAY:
                raise _Invalid('annual_gap_or_overlap', 'Annual periods have a gap or overlap')
    return chosen, currency


def _part(row: _Row, start: date | None = None, sign: int = 1, label: str = '') -> dict:
    return dict(start=(start or row.start).isoformat() if (start or row.start) else None,
                end=row.end.isoformat(), fcf=row.amount, coefficient=sign,
                currency=row.currency, label=label or row.period)


def _result(amount: Decimal, currency: str, method: str, start: date | None,
            end: date, components: list[dict], warnings: list[str],
            latest_available: date, **extra: Any) -> dict:
    exact = bool(start and start == _shift_year(end, -5) + DAY)
    return dict(status='ok', fcf_sum=amount, cumulative_fcf=amount, currency=currency,
                method=method, period_start=start.isoformat() if start else None,
                period_end=end.isoformat(), latest_available_date=latest_available.isoformat(),
                exact_calendar_60_months=exact, components=components,
                passes=amount > ZERO, passed=amount > ZERO,
                reason_codes=[], warnings=warnings, **extra)


def _calendar_fy(start: date | None, end: date) -> bool:
    return bool(start and _month_end(end) and start == _shift_month_end(end, -12) + DAY)


def _year_candidates(interims: list[_Row], start: date, end: date,
                     currency: str, annual: _Row | None) -> dict[date, set[Decimal]]:
    """Plausible cumulative values; ambiguous Q2/Q3 values stay ambiguous.

    Calendar-quarter boundaries must be verifiable. A complete Q1..Q4 set is
    checked against that fiscal year's annual FCF. Explicit YTD and interval
    starts can establish values without requiring a full quartet.
    """
    if not _calendar_fy(start, end):
        return {}
    expected_ends = {q: _shift_month_end(end, (q - 4) * 3) for q in range(1, 5)}
    by_quarter: dict[int, _Row] = {}
    for row in interims:
        if not start <= row.end <= end or row.currency != currency:
            continue
        qs = [q for q, endpoint in expected_ends.items() if row.end == endpoint]
        if not qs:
            continue
        q = qs[0]
        if row.period.startswith('Q') and row.period != f'Q{q}':
            continue
        if q in by_quarter and row != by_quarter[q]:
            # Different labels for the same endpoint cannot be blindly merged.
            return {}
        by_quarter[q] = row
    if not by_quarter:
        return {end: {annual.amount}} if annual else {}
    # Build complete prefix interpretations; each tuple is cumulative values.
    states: list[tuple[Decimal, ...]] = [()]
    output: dict[date, set[Decimal]] = {}
    for q in range(1, 5):
        row = by_quarter.get(q)
        if not row:
            break
        quarter_start = start if q == 1 else expected_ends[q - 1] + DAY
        choices: list[str] = []
        if row.start is not None:
            if row.start == start:
                choices = ['ytd']
            elif row.start == quarter_start:
                choices = ['quarter']
            else:
                break
            if row.basis in ('quarter', 'ytd') and row.basis not in choices and q != 1:
                break
        elif q == 1:
            choices = ['ytd']
        elif row.basis in ('quarter', 'ytd'):
            choices = [row.basis]
        elif row.basis == 'unknown':
            choices = ['quarter', 'ytd']
        else:
            break
        states = [state + ((row.amount if choice == 'ytd' else (state[-1] if state else ZERO) + row.amount),)
                  for state, choice in product(states, choices)]
        output[row.end] = {state[-1] for state in states}
    if states and len(states[0]) == 4 and annual:
        states = [state for state in states if _close(state[-1], annual.amount)]
        if not states:
            return {}
        output = {expected_ends[q]: {state[q - 1] for state in states} for q in range(1, 5)}
    # Explicit YTD is standalone and does not require earlier quarter rows.
    for q, row in by_quarter.items():
        if (row.start == start and (row.basis != 'quarter' or q == 1)) or (row.start is None and row.basis == 'ytd') or (q == 1 and row.start is None and row.basis in ('unknown', 'quarter', 'ytd')):
            output[row.end] = {row.amount}
    if annual:
        output[end] = {annual.amount}
    return output


def _unique(values: set[Decimal] | None) -> Decimal | None:
    return next(iter(values)) if values and len(values) == 1 else None


def _twenty_quarters(interims: list[_Row], latest: date, annual: list[_Row],
                     ttms: list[_Row], starts: dict[date, date | None]) -> dict | None:
    """A direct alternative requiring twenty explicit contiguous intervals."""
    target_start = _shift_year(latest, -5) + DAY
    candidates = []
    for row in interims:
        if row.start and row.end <= latest and row.start >= target_start:
            length = (row.end - row.start).days + 1
            if 70 <= length <= 112 and row.basis in ('quarter', 'unknown'):
                candidates.append(row)
    candidates.sort(key=lambda r: r.end)
    if len(candidates) != 20 or candidates[0].start != target_start or candidates[-1].end != latest:
        return None
    currency = candidates[0].currency
    for index, row in enumerate(candidates):
        if row.currency != currency or (index and row.start != candidates[index - 1].end + DAY):
            return None
    for yearly in annual:
        year_start = starts.get(yearly.end)
        covered = [r for r in candidates if year_start and r.start >= year_start and r.end <= yearly.end]
        if not year_start:
            # Explicit quarter bounds can supply a comparison interval even
            # when the annual statement itself omits its start date. This is
            # only a reconciliation, never an invented annual coverage claim.
            trailing = [r for r in candidates if r.end <= yearly.end][-4:]
            if len(trailing) == 4 and trailing[-1].end == yearly.end and 350 <= (yearly.end - trailing[0].start).days + 1 <= 378:
                covered = trailing
                year_start = trailing[0].start
        if covered and covered[0].start == year_start and covered[-1].end == yearly.end:
            if yearly.currency != currency or not _close(sum((r.amount for r in covered), ZERO), yearly.amount):
                return None
    for ttm in ttms:
        if ttm.end == latest:
            covered = candidates[-4:]
            if covered[0].start == _shift_year(latest, -1) + DAY:
                if ttm.currency != currency or not _close(sum((r.amount for r in covered), ZERO), ttm.amount):
                    return None
    return _result(sum((r.amount for r in candidates), ZERO), currency,
                   'rolling_20_verified_quarters', target_start, latest,
                   [_part(r) for r in candidates], [], latest)


def cumulative_fcf(annual_rows: Iterable[Mapping[str, Any]] | None,
                   interim_rows: Iterable[Mapping[str, Any]] | None,
                   ttm_rows: Iterable[Mapping[str, Any]] | None = None,
                   symbol: str | None = None, as_of: Any = None) -> dict:
    """Select a non-overlapping five-year FCF interval, retaining losses.

    Preference: five FY minus oldest matching YTD plus current YTD; twenty
    explicitly bounded verified quarters; otherwise five complete FY, visibly
    labelled. Reported TTM is only a cross-check / source for deriving YTD,
    never a sixth year added to five annual figures. A 52/53-week fiscal set
    may use the five-FY fallback but is never described as exact 60 months.
    ``as_of`` excludes period ends and known filings later than that date.
    """
    annual_rows, interim_rows, ttm_rows = list(annual_rows or []), list(interim_rows or []), list(ttm_rows or [])
    warnings: list[str] = []
    try:
        cutoff = _date(as_of, 'as_of') if as_of is not None else datetime.now(timezone.utc).date()
        annual, predecessor, annual_parse_error = _selected_annual_rows(annual_rows, cutoff, symbol, warnings)
    except _Invalid as error:
        return MissingData(error.reason, error.detail)
    # A bad unrelated observation must not erase a reconstructible newest
    # window (which could otherwise turn a known failure into a fallback pass).
    interims = _optional_rows(interim_rows, 'interim', cutoff, symbol, warnings)
    ttms = _optional_rows(ttm_rows, 'ttm', cutoff, symbol, warnings)
    if not annual and not interims:
        if annual_parse_error:
            return MissingData(annual_parse_error.reason, annual_parse_error.detail, warnings=warnings)
        return MissingData('no_usable_cashflow', 'No usable annual or quarterly cash-flow statements', warnings=warnings)
    known_dates = [r.end for r in annual + interims + ttms]
    for raw in annual_rows + interim_rows + ttm_rows:
        try:
            endpoint = _date(_first(raw, 'period_end', 'endDate', 'date'), 'statement end')
            filing = _first(raw, 'acceptedDate', 'filingDate', 'fillingDate')
            if endpoint <= cutoff and (not filing or _date(filing, 'filing date') <= cutoff):
                known_dates.append(endpoint)
        except (_Invalid, AttributeError):
            pass
    latest = max(known_dates)
    starts = _annual_starts(annual, predecessor)
    annual_error = annual_parse_error
    chosen, currency = [], ''
    if not annual_parse_error:
        try:
            chosen, currency = _five_annual(annual, starts)
        except _Invalid as error:
            annual_error = error
    if chosen:
        first, last = chosen[0], chosen[-1]
        # Never shift a five-FY fallback backward when a subsequently observed
        # fiscal year proves an intervening completed annual year is missing.
        # The current wall clock is NOT evidence that a filing should exist.
        label_gap = (last.fiscal_year is not None and any(
            row.end > last.end and row.fiscal_year is not None
            and row.fiscal_year > last.fiscal_year + 1 for row in interims + ttms))
        observed_gap = latest > last.end + timedelta(days=378)
        calendar_gap = (_calendar_fy(starts[last.end], last.end)
                        and latest > _shift_month_end(last.end, 12))
        if label_gap or observed_gap or calendar_gap:
            quarters = _twenty_quarters(interims, latest, annual, ttms, starts)
            if quarters:
                quarters['warnings'] = warnings
                quarters['fallback'] = False
                return quarters
            return MissingData(
                'missing_latest_completed_fiscal_year',
                f'Observed cash-flow data through {latest} belongs beyond the fiscal year immediately after '
                f'the latest complete annual statement ({last.end}); an intervening completed FY is missing',
                warnings=warnings, latest_available_date=latest.isoformat())
        first_start = starts[first.end]
        annual_sum = sum((r.amount for r in chosen), ZERO)
        parts = [_part(r, starts[r.end], label='FY') for r in chosen]
        calendar_years = all(_calendar_fy(starts[r.end], r.end) for r in chosen)
        if latest == last.end:
            if not calendar_years:
                quarters = _twenty_quarters(interims, latest, annual, ttms, starts)
                if quarters:
                    quarters['warnings'] = warnings
                    quarters['fallback'] = False
                    return quarters
            if not calendar_years:
                warnings.append('Full fiscal years are not verified as exact calendar years; dates shown without claiming exact calendar 60 months')
            method = 'last_5_full_fiscal_years'
            return _result(annual_sum, currency, method, first_start, last.end,
                           parts, warnings, latest, fallback=not calendar_years,
                           fiscal_year_ends=[r.end.isoformat() for r in chosen])
        if calendar_years and first_start and last.end < latest <= _shift_month_end(last.end, 12):
            current_start, current_end = last.end + DAY, _shift_month_end(last.end, 12)
            old_end = _shift_year(latest, -5)
            previous_end = _shift_year(latest, -1)
            if first_start <= old_end <= first.end:
                oldest = _year_candidates(interims, first_start, first.end, currency, first)
                current = _year_candidates(interims, current_start, current_end, currency, None)
                previous = _year_candidates(interims, starts[last.end], last.end, currency, last)
                old_values = oldest.get(old_end, set())
                current_values = current.get(latest, set())
                previous_values = previous.get(previous_end, set())
                reported_ttm = [r for r in ttms if r.end == latest and r.currency == currency
                                and (r.start is None or r.start == previous_end + DAY)]
                used_ttm = False
                if reported_ttm:
                    ttm = reported_ttm[-1]
                    derived = {ttm.amount - last.amount + p for p in previous_values}
                    if current_values:
                        matched = {c for c in current_values if any(_close(c, d) for d in derived)}
                        if derived and not matched:
                            warnings.append('Latest interim cash flow disagrees with the reported TTM reconciliation')
                            current_values = set()
                        elif matched:
                            current_values = matched
                            used_ttm = True
                    elif derived:
                        current_values = derived
                        used_ttm = True
                old_ytd, current_ytd = _unique(old_values), _unique(current_values)
                if old_ytd is None:
                    warnings.append('Oldest matching YTD is missing, has incompatible currency/boundaries, or cannot be distinguished from discrete-quarter data')
                if current_ytd is None:
                    warnings.append('Latest YTD is missing or ambiguous; a reported TTM can derive it only with the prior comparable YTD in the same currency')
                if old_ytd is not None and current_ytd is not None:
                    amount = annual_sum - old_ytd + current_ytd
                    parts += [dict(start=first_start.isoformat(), end=old_end.isoformat(),
                                   fcf=old_ytd, coefficient=-1, currency=currency, label='oldest_YTD'),
                              dict(start=current_start.isoformat(), end=latest.isoformat(),
                                   fcf=current_ytd, coefficient=1, currency=currency, label='latest_YTD')]
                    return _result(amount, currency, 'rolling_5fy_minus_old_ytd_plus_latest_ytd',
                                   old_end + DAY, latest, parts, warnings, latest,
                                   fallback=False, ttm_used_for_ytd=used_ttm,
                                   fiscal_year_ends=[r.end.isoformat() for r in chosen])
    quarters = _twenty_quarters(interims, latest, annual, ttms, starts)
    if quarters:
        quarters['warnings'] = warnings
        quarters['fallback'] = False
        return quarters
    if chosen:
        if latest > _shift_year(last.end, 1):
            warnings.append('The newest cash-flow endpoint is beyond the fiscal year immediately following the latest complete annual statement')
        warnings.append('Exact rolling coverage could not be established; using the last five complete fiscal years')
        if not calendar_years:
            warnings.append('Non-calendar or 52/53-week fiscal periods: dates shown; do not interpret as exact calendar 60 months')
        return _result(annual_sum, currency, 'last_5_full_fiscal_years', first_start,
                       last.end, parts, warnings, latest, fallback=True,
                       fiscal_year_ends=[r.end.isoformat() for r in chosen])
    if annual_error:
        return MissingData(annual_error.reason, annual_error.detail, warnings=warnings,
                           latest_available_date=latest.isoformat())
    return MissingData('insufficient_verified_history', 'Neither five complete FY nor twenty verified quarters are available', warnings=warnings)
