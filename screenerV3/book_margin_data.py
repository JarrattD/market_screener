"""Market-cap-first annual income and current book inputs, without extra gates."""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
import asyncio

from .book_margin_provider import (
    FMPClient as ProviderClient, FMPError, FMPAuthenticationError, Issue,
    utc_now, _map_bounded, _timestamp,
)

INCOME_FIELDS = ('reportedCurrency', 'revenue', 'grossProfit', 'operatingIncome', 'ebit')
BALANCE_FIELDS = (
    'reportedCurrency', 'commonStockEquity', 'totalStockholdersEquity', 'totalEquity',
    'minorityInterest', 'noncontrollingInterest', 'preferredStock', 'totalAssets', 'totalLiabilities',
    'cashAndCashEquivalents', 'shortTermInvestments', 'cashAndShortTermInvestments',
    'netReceivables', 'inventory', 'goodwill', 'intangibleAssets',
    'goodwillAndIntangibleAssets', 'propertyPlantEquipmentNet', 'otherAssets',
)


def available_statements(rows, symbol, as_of=None):
    """Only valid, completed, publicly filed periods for this exact security."""
    now = as_of or datetime.now(timezone.utc)
    today = now.date() if isinstance(now, datetime) else now
    result = []
    for row in rows:
        if str(row.get('symbol') or symbol) != symbol:
            continue
        try:
            end = date.fromisoformat(str(row.get('date') or '')[:10])
        except ValueError:
            continue
        if end > today:
            continue
        invalid = False
        for key in ('filingDate', 'fillingDate', 'acceptedDate'):
            value = row.get(key)
            if value not in (None, ''):
                stamp = _timestamp(value)
                if stamp is None or stamp.date() > today or (isinstance(now, datetime) and stamp > now):
                    invalid = True
        if not invalid:
            result.append(row)
    return result


def latest_statement(rows, symbol, annual_only=False, as_of=None):
    """Newest period, then verified newest restatement; no API-order guesses.

    An equal-date conflicting record without an ordered filing timestamp is
    unresolved, including equity and income fields, not only cash/debt fields. Conflicting
    optional diagnostics are blanked and qualified, never an extra screen gate.
    """
    valid_periods = ('FY', 'ANNUAL') if annual_only else ('FY', 'ANNUAL', 'Q1', 'Q2', 'Q3', 'Q4', 'H1', 'H2')
    candidates = [r for r in available_statements(rows, symbol, as_of)
                  if str(r.get('period') or '').upper() in valid_periods]
    if not candidates:
        return None
    end = max(str(r['date'])[:10] for r in candidates)
    candidates = [r for r in candidates if str(r['date'])[:10] == end]
    def normalized(value):
        if value is None or value == '': return None
        try:
            value = Decimal(str(value))
            return value if value.is_finite() else str(value)
        except InvalidOperation: return str(value)
    fields = ('reportedCurrency', 'revenue', 'grossProfit') if annual_only else (
        'reportedCurrency', 'commonStockEquity', 'totalStockholdersEquity',
        'totalEquity', 'minorityInterest', 'noncontrollingInterest', 'preferredStock')
    optional = INCOME_FIELDS + ('fiscalYear', 'calendarYear') if annual_only else BALANCE_FIELDS
    def signature(row): return tuple(normalized(row.get(k)) for k in fields)
    def revision(row): return _timestamp(row.get('acceptedDate') or row.get('filingDate') or row.get('fillingDate'))
    has_unknown_revision = any(revision(r) is None for r in candidates)
    diagnostic_pool = list(candidates)
    if has_unknown_revision and len({signature(r) for r in candidates}) > 1:
        return None
    unknown = datetime.min.replace(tzinfo=timezone.utc)
    newest = max(revision(r) or unknown for r in candidates)
    candidates = [r for r in candidates if (revision(r) or unknown) == newest]
    if len({signature(r) for r in candidates}) > 1:
        return None
    chosen = dict(max(candidates, key=lambda r: str(r.get('period') or '').upper() in ('FY', 'ANNUAL')))
    conflicts = []
    for key in optional:
        if key not in fields and len({normalized(r.get(key)) for r in (diagnostic_pool if has_unknown_revision else candidates)}) > 1:
            chosen[key] = None
            conflicts.append(key)
    if conflicts:
        chosen['_diagnostic_conflicts'] = conflicts
    return chosen


def resolved_annual_history(rows, symbol):
    """Choose newest verified revision per FY; retain conflicts as unknown history."""
    groups, result = {}, []
    for row in rows:
        if str(row.get('period') or '').upper() not in ('FY', 'ANNUAL'):
            continue
        key = row.get('fiscalYear') or row.get('calendarYear') or str(row.get('date') or '')[:4]
        groups.setdefault(str(key), []).append(row)
    for key in sorted(groups, reverse=True):
        chosen = latest_statement(groups[key], symbol, annual_only=True)
        result.extend([chosen] if chosen is not None else groups[key])
    return result


@dataclass
class AnnualIncome:
    latest: dict | None
    history: list = field(default_factory=list)
    fetched_at: str = ''
    error: str | None = None


class FMPClient(ProviderClient):
    async def statements(self, symbol, endpoint, period, limit):
        try:
            rows = await self._compatible('/stable/' + endpoint,
                '/api/v3/' + endpoint + '/' + quote(symbol, safe=''),
                {'symbol': symbol, 'period': period, 'limit': limit})
            return available_statements(rows, symbol), None
        except FMPAuthenticationError:
            raise
        except FMPError as exc:
            if self._fatal is not None:
                raise self._fatal
            return [], exc.reason

    async def fetch_annual_income(self, symbols):
        result, issues = {}, []
        symbols = sorted(set(symbols))
        async def one(symbol):
            rows, error = await self.statements(symbol, 'income-statement', 'annual', 8)
            latest = latest_statement(rows, symbol, annual_only=True)
            result[symbol] = AnnualIncome(latest, rows, utc_now(), error)
            if latest is None:
                issues.append(Issue(symbol, 'annual_income', error or 'missing_or_conflicting_latest_annual_income'))
        await _map_bounded(symbols, one, self.concurrency)
        self.metrics['annual_income_candidates'] = len(symbols)
        self.health['annual_income_available'] = any(r.latest is not None for r in result.values())
        if symbols and not self.health['annual_income_available']:
            self.health['systemic_failure'] = True
        return result, sorted(issues, key=lambda i: (i.symbol, i.reason))

    async def fetch_balances(self, symbols):
        result, issues = {}, []
        symbols = sorted(set(symbols))
        async def one(symbol):
            annual, quarter = await asyncio.gather(
                self.statements(symbol, 'balance-sheet-statement', 'annual', 3),
                self.statements(symbol, 'balance-sheet-statement', 'quarter', 4))
            errors = [error for _, error in (annual, quarter) if error]
            result[symbol] = None if errors else latest_statement(annual[0] + quarter[0], symbol)
            if result[symbol] is None:
                issues.append(Issue(symbol, 'balance', ';'.join(sorted(set(errors))) or 'missing_or_conflicting_latest_balance'))
        await _map_bounded(symbols, one, self.concurrency)
        self.metrics['balance_candidates'] = len(symbols)
        self.health['balance_available'] = any(r is not None for r in result.values())
        if symbols and not self.health['balance_available']:
            self.health['systemic_failure'] = True
        return result, sorted(issues, key=lambda i: (i.symbol, i.reason))
