"""Independent global <=US$75m, <=0.5x ordinary book, >=30% annual GM screen."""
import argparse
import asyncio
from collections import Counter
from datetime import date, datetime, timezone
import json
import sys
import time
from zoneinfo import ZoneInfo
from screenerV3.book_margin_calculations import screen_market_cap, screen_book_value, annual_margin, five_year_margins, asset_diagnostics
from screenerV3.book_margin_output import open_destination, publish, resume_complete
from screenerV3.book_margin_data import resolved_annual_history

CONFIG = {'version': 1, 'market_cap_usd_min_exclusive': 0, 'market_cap_usd_max_inclusive': 75000000,
          'ordinary_book_equity_positive': True, 'price_to_ordinary_book_max_inclusive': '0.5',
          'latest_completed_annual_gross_margin_min_inclusive': '0.30', 'revenue_positive': True,
          'additional_investment_filters': [], 'balance': 'Newest completed annual or interim',
          'gross_margin': 'Newest completed publicly filed FY; 5Y history is optional'}
HEADERS = ['Ticker', 'Company', 'Country', 'Exchange', 'Market cap USD', 'P/B',
    'Ordinary attributable book equity', 'Book currency', 'Latest FY gross margin',
    'Latest FY revenue', 'Latest FY gross profit', 'Income currency', 'Operating income',
    'Operating margin', 'EBIT (separate provider field)', 'FY end', 'FY filing / accepted date',
    'FY age days', 'Balance end', 'Balance filing / accepted date', 'Balance age days',
    'Original market cap', 'Market-cap currency', 'Market-cap unit basis', 'Market-cap as of',
    'Profile fetched UTC', 'Cap-to-USD FX as of', 'Ordinary equity basis', 'Preferred stock',
    'Noncontrolling interest (diagnostic)', 'Total assets', 'Total liabilities',
    'Cash and short-term investments', 'Net receivables', 'Inventory', 'Goodwill',
    'Intangible assets', 'Property, plant and equipment, net', 'Other assets',
    'Asset composition diagnostics JSON', 'Up to 5 FY gross margins JSON',
    'Freshness / data qualifications', 'Assessment UTC', 'Source', 'Security identity',
    'Possible issuer aliases', 'Cap-to-book FX as of', 'Cap-to-book FX rate']

def health(client):
    fatal = getattr(client, '_fatal', None)
    if fatal is not None: raise fatal

def text(value): return json.dumps(value, sort_keys=True, default=str, separators=(',', ':'))
def reasons(result):
    value = result.get('reasons') or result.get('reason_codes') or result.get('reason') or []
    return '; '.join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
def audit_row(symbol, status, reason=''): return [symbol, status, reason, 'FMP']
def filing(row): return row.get('acceptedDate') or row.get('filingDate') or row.get('fillingDate') or ''
def age_days(row, assessed):
    try: return (assessed.date() - date.fromisoformat(str(row.get('date'))[:10])).days
    except (ValueError, TypeError): return None

async def run(service_account, spreadsheet_id, client=None):
    sheet = open_destination(service_account, spreadsheet_id)
    existing = resume_complete(sheet, datetime.now(ZoneInfo('Europe/London')).date(), CONFIG)
    if existing:
        print('Existing dated book-value results verified; no duplicate run', flush=True)
        return existing
    if client is None:
        from screenerV3.book_margin_data import FMPClient
        async with FMPClient() as session: return await screen(session, sheet)
    return await screen(client, sheet)

async def screen(client, sheet):
    started, assessed = time.perf_counter(), datetime.now(timezone.utc)
    batch_date = assessed.astimezone(ZoneInfo('Europe/London')).date()
    print('Loading fresh global universe and profiles', flush=True)
    universe = await client.fetch_universe(); health(client)
    profiles, issues = await client.fetch_profiles(universe); health(client)
    audit, eligible = {}, []
    for item in universe:
        symbol, p = item['symbol'], profiles.get(item['symbol'])
        if p is None: audit[symbol] = audit_row(symbol, 'unresolved', 'Profile unavailable')
        elif p.get('security_eligible') is not True:
            audit[symbol] = audit_row(symbol, 'excluded_security' if p.get('security_eligible') is False else 'unresolved',
                                      p.get('security_reason', 'Listing eligibility unresolved'))
        else: eligible.append(symbol)
    currencies = {profiles[s].get('cap_currency') for s in eligible} - {None, ''}
    needed = currencies | {'USD'} if currencies - {'USD'} else set()
    rates, fx_issues = await client.fetch_fx(needed) if needed else ({}, []); health(client)
    issues.extend(fx_issues)
    caps, small = {}, []
    for symbol in eligible:
        cap = screen_market_cap(profiles[symbol], rates); caps[symbol] = cap
        if cap['status'] != 'ok': audit[symbol] = audit_row(symbol, 'unresolved', reasons(cap))
        elif not cap['passes']: audit[symbol] = audit_row(symbol, 'rejected', 'Market cap exceeds US$75m')
        else: small.append(symbol)
    print('Reading annual income only for verified market-cap candidates', flush=True)
    incomes, income_issues = await client.fetch_annual_income(small); health(client)
    issues.extend(income_issues)
    margins, high_margin = {}, []
    for symbol in small:
        income = incomes.get(symbol)
        if income is None or income.latest is None:
            audit[symbol] = audit_row(symbol, 'unresolved', 'Latest completed annual income missing or conflicting'); continue
        margin = annual_margin(income.latest); margins[symbol] = margin
        if margin['status'] != 'ok': audit[symbol] = audit_row(symbol, 'unresolved', reasons(margin))
        elif not margin['passes']: audit[symbol] = audit_row(symbol, 'rejected', 'Latest annual gross margin below 30% or revenue nonpositive')
        else: high_margin.append(symbol)
    print('Reading current balance sheets only for annual-margin candidates', flush=True)
    balances, balance_issues = await client.fetch_balances(high_margin); health(client)
    issues.extend(balance_issues)
    needed = {b.get('reportedCurrency') for b in balances.values() if b} - {None, '', 'USD'}
    if needed:
        more_rates, more_issues = await client.fetch_fx(needed | {'USD'}); rates.update(more_rates); issues.extend(more_issues)
    health(client)
    rows, calculations = [HEADERS], {}
    for symbol in high_margin:
        p, b = profiles[symbol], balances.get(symbol)
        if b is None:
            audit[symbol] = audit_row(symbol, 'unresolved', 'Latest annual/interim balance missing or conflicting'); continue
        book = screen_book_value(p, b, rates)
        if book['status'] != 'ok': audit[symbol] = audit_row(symbol, 'unresolved', reasons(book)); continue
        if not book['passes']:
            audit[symbol] = audit_row(symbol, 'rejected', 'Ordinary equity nonpositive or P/B exceeds 0.5'); continue
        history, cap, margin = incomes[symbol], caps[symbol], margins[symbol]
        income = history.latest
        trend, assets = five_year_margins(resolved_annual_history(history.history, symbol)), asset_diagnostics(b)
        ia, ba = age_days(income, assessed), age_days(b, assessed)
        q = []
        if ia is not None and ia > 550: q.append('STALE FY: newest provider annual period is over 550 days old; verify newer filings')
        if ba is not None and ba > 400: q.append('STALE BALANCE: newest provider balance is over 400 days old; verify newer filings')
        if not p.get('as_of'): q.append('Market-cap timestamp unavailable; fetched time is not valuation time')
        if not filing(income): q.append('FY filing timestamp unavailable')
        if not filing(b): q.append('Balance filing timestamp unavailable')
        if history.error: q.append('Annual history: ' + history.error)
        q.extend(assets.get('reasons', []))
        for label, record in (('Income', income), ('Balance', b)):
            if record.get('_diagnostic_conflicts'):
                q.append(label + ' conflicting optional fields left blank: ' + ', '.join(record['_diagnostic_conflicts']))
        q.append('Ordinary book includes goodwill/intangibles; not tangible book or liquidation value')
        rows.append([symbol, p.get('companyName', ''), p.get('country', ''), p.get('exchangeShortName', p.get('exchange', '')),
            cap.get('market_cap_usd'), book.get('price_to_book'), book.get('book_equity'), book.get('book_currency'),
            margin.get('gross_margin'), margin.get('revenue'), margin.get('gross_profit'), income.get('reportedCurrency'),
            margin.get('operating_income'), margin.get('operating_margin'), margin.get('ebit'),
            income.get('date'), filing(income), ia, b.get('date'), filing(b), ba,
            p.get('cap_amount'), p.get('cap_currency'), p.get('cap_currency_basis'), p.get('as_of'), p.get('fetched_at'), cap.get('fx_as_of'),
            book.get('equity_basis'), book.get('preferred_stock'), book.get('noncontrolling_interest'),
            assets.get('total_assets'), assets.get('total_liabilities'), assets.get('cash_and_short_term_investments'),
            assets.get('receivables'), assets.get('inventory'), assets.get('goodwill'), assets.get('intangibles'),
            assets.get('property_plant_equipment'), assets.get('other_assets'), text(assets), text(trend), '; '.join(q), assessed.isoformat(),
            'https://site.financialmodelingprep.com/financial-summary/' + symbol,
            p.get('security_identity', 'SYMBOL:' + symbol), '', book.get('cap_to_book_fx_as_of'), book.get('cap_to_book_fx_rate')])
        calculations[symbol] = {'market_cap': cap, 'book': book, 'margin': margin, 'trend': trend, 'assets': assets}
        audit[symbol] = audit_row(symbol, 'match', 'Market cap <=US$75m; ordinary equity >0 and P/B <=0.5; annual GM >=30%')
    unique, identity_index, aliases_index = {}, HEADERS.index('Security identity'), HEADERS.index('Possible issuer aliases')
    for row in sorted(rows[1:], key=lambda r: (r[2], r[0])):
        identity = row[identity_index]
        if identity in unique: audit[row[0]] = audit_row(row[0], 'duplicate_listing', 'Verified same security as ' + unique[identity][0])
        else: unique[identity] = row
    rows, issuers = [HEADERS] + list(unique.values()), {}
    for row in rows[1:]:
        p = profiles[row[0]]; cik = str(p.get('cik') or '').lstrip('0')
        key = 'CIK:' + cik if cik else 'NAME:' + str(p.get('companyName', '')).casefold().strip() + ':' + str(p.get('country', ''))
        issuers.setdefault(key, []).append(row)
    for group in issuers.values():
        if len(group) > 1:
            for row in group: row[aliases_index] = 'Possible issuer aliases: ' + ', '.join(r[0] for r in group if r is not row)
    expected = {item['symbol'] for item in universe}
    if set(audit) != expected: raise ValueError('Universe coverage accounting mismatch')
    if not universe or not profiles or getattr(client, 'health', {}).get('systemic_failure'):
        raise ValueError('Systemic provider failure; no completed result published')
    issues.extend(client.diagnostics); counts = Counter(row[1] for row in audit.values())
    coverage = {'status': 'complete_with_unresolved' if counts['unresolved'] or issues else 'complete',
        'provider_universe': len(expected), **dict(counts), 'matched_rows': len(rows)-1,
        'market_cap_candidates': len(small), 'annual_margin_candidates': len(high_margin), 'assessment_utc': assessed.isoformat(),
        'data_issue_count': len(issues), 'universe_sources': client.universe_source,
        'data_stage_seconds': round(time.perf_counter()-started, 3), 'request_metrics': text(getattr(client, 'metrics', {}))}
    audit_rows = [['Ticker', 'Status', 'Reason', 'Source']] + sorted(audit.values())
    audit_rows += [[i.symbol, 'data_issue', i.stage + ': ' + i.reason, 'FMP'] for i in issues]
    records = [['Ticker', 'Record', 'Provider data / calculation JSON', 'Retrieved UTC']]
    for row in rows[1:]:
        symbol, income = row[0], incomes[row[0]]
        for kind, observation in [('Profile', profiles[symbol]), ('Latest balance sheet', balances[symbol]), ('Screen calculations', calculations[symbol])]:
            records.append([symbol, kind, text(observation), income.fetched_at])
        for observation in income.history: records.append([symbol, 'Annual income', text(observation), income.fetched_at])
    for currency, rate in rates.items(): records.append([currency, 'FX', text(rate.to_dict()), rate.fetched_at])
    health(client)
    receipt = publish(sheet, batch_date, rows, audit_rows, coverage, CONFIG, records)
    print('New-screen private results and coverage verified', flush=True)
    return receipt

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-account', required=True); parser.add_argument('--spreadsheet-id', required=True)
    args = parser.parse_args()
    try: asyncio.run(run(args.service_account, args.spreadsheet_id))
    except Exception as exc:
        print('Book-value screen failed before verified completion: ' + type(exc).__name__, file=sys.stderr); return 1
    return 0
if __name__ == '__main__': raise SystemExit(main())
