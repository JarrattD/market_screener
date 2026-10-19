"""Resolve the actual fields consumed by the legacy engines, not API order."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from .data_freshness import DataFreshnessError, quote_timestamp

FIELDS = ('freeCashFlow','commonStockRepurchased','cashAtEndOfPeriod',
          'totalCurrentAssets','totalLiabilities','netDebt','totalDebt',
          'cashAndCashEquivalents','cashAndShortTermInvestments')

def number(value):
    if value is None or value == '':return None
    try:
        result=Decimal(str(value));return result if result.is_finite() else str(value)
    except InvalidOperation:return str(value)

def signature(row):
    return (str(row.get('reportedCurrency') or ''), *(number(row.get(f)) for f in FIELDS),
            str(row.get('startDate') or row.get('periodStartDate') or '')[:10])

def resolve(rows, ticker, annual=False):
    now=datetime.now(timezone.utc)
    candidates=[]
    for row in rows or []:
        if row.get('symbol',ticker)!=ticker:continue
        if annual and str(row.get('period','')).upper() not in ('FY','ANNUAL'):continue
        try:period=date.fromisoformat(str(row.get('date',''))[:10])
        except ValueError:continue
        if period>now.date():raise DataFreshnessError('Statement period is future-dated')
        revision=quote_timestamp(row.get('acceptedDate') or row.get('filingDate') or row.get('fillingDate'))
        if revision and revision>now+timedelta(hours=1):raise DataFreshnessError('Statement filing/revision is future-dated')
        candidates.append((period,revision,row))
    if not candidates:raise DataFreshnessError('Statement missing')
    end=max(x[0] for x in candidates);latest=[x for x in candidates if x[0]==end]
    if any(x[1] is None for x in latest) and len({signature(x[2]) for x in latest})>1:
        raise DataFreshnessError('Conflicting undated financial revisions')
    newest=max(x[1] or datetime.min.replace(tzinfo=timezone.utc) for x in latest)
    latest=[x for x in latest if (x[1] or datetime.min.replace(tzinfo=timezone.utc))==newest]
    if len({signature(x[2]) for x in latest})>1:raise DataFreshnessError('Conflicting same-time financial revisions')
    return latest[0][2]

def annual_history(rows,ticker):
    groups={}
    for row in rows or []:
        if str(row.get('period','')).upper() not in ('FY','ANNUAL'):continue
        try:year=date.fromisoformat(str(row.get('date',''))[:10]).year
        except ValueError:continue
        groups.setdefault(year,[]).append(row)
    years=sorted(groups,reverse=True)[:5]
    if len(years)!=5 or any(years[i]-years[i+1]!=1 for i in range(4)):
        raise DataFreshnessError('Five completed consecutive annual cashflow periods unavailable')
    selected=[resolve(groups[y],ticker,True) for y in years]
    currencies={str(r.get('reportedCurrency') or '').strip().upper() for r in selected}
    if '' in currencies or len(currencies)!=1:
        raise DataFreshnessError('Annual cashflow currencies missing or inconsistent')
    dates=[date.fromisoformat(str(r['date'])[:10]) for r in selected]
    if any(not 350 <= (dates[i]-dates[i+1]).days <=380 for i in range(4)):
        raise DataFreshnessError('Irregular annual cashflow periods')
    for row,end in zip(selected,dates):
        start=row.get('startDate') or row.get('periodStartDate')
        if start and not 350 <= (end-date.fromisoformat(str(start)[:10])).days+1 <=380:
            raise DataFreshnessError('Partial annual cashflow period')
    return selected
