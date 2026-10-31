"""Independent global annual revenue/margin and latest-balance net-cash screen."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import sys
from screenerV3.completion import london_batch_date
from screenerV3.data_freshness import POLICY, require_statement_current, DataFreshnessError
from screenerV3.book_margin_calculations import annual_margin
from screenerV3.revenue_net_cash_calculations import assess_revenue, net_cash
from screenerV3.revenue_net_cash_output import open_destination, publish, resume_complete

CONFIG = {'version': 2, 'market_cap_usd_max_exclusive': '100000000', 'market_cap_to_latest_fy_revenue_max_inclusive': '0.10',
 'latest_fy_gross_margin_min_inclusive': '0.30', 'annual_revenue_positive': True,
 'net_cash_min_exclusive': '0', 'balance': 'newest completed annual or interim',
 'whole_issuer_market_cap_required': True, 'additional_financial_filters': [],
 'appearance_cooldown': '12 calendar months; published appearances only; anniversary eligible',
 'freshness_policy': POLICY}
HEADERS = ['Ticker', 'Company', 'Country', 'Exchange', 'Market cap / FY revenue',
 'FY gross margin', 'Net cash', 'Market cap in income currency', 'FY revenue',
 'FY gross profit', 'Income currency', 'FY end', 'FY filing / accepted date',
 'Cash and short-term investments', 'Total debt', 'Balance currency', 'Balance end',
 'Balance filing / accepted date', 'Original market cap', 'Market-cap currency',
 'Market-cap unit basis', 'Quote UTC', 'Cap-to-income FX rate', 'Cap-to-income FX as of',
 'Cash source', 'Security identity', 'Possible issuer aliases', 'Assessment UTC',
 'Source', 'Qualifications']

def text(value): return json.dumps(value, sort_keys=True, default=str, separators=(',', ':'))
def health(client):
    if getattr(client, '_fatal', None) is not None: raise client._fatal

def reason(value): return '; '.join(map(str, value.get('reasons') or value.get('reason_codes') or []))
def filing(row): return row.get('acceptedDate') or row.get('filingDate') or row.get('fillingDate') or ''

async def run(service_account, spreadsheet_id, client=None):
    sheet = open_destination(service_account, spreadsheet_id)
    batch_date = london_batch_date()
    receipt = resume_complete(sheet, batch_date, CONFIG)
    if receipt: return receipt
    if client is None:
        from screenerV3.revenue_net_cash_data import FMPClient
        async with FMPClient() as session: return await screen(session, sheet, batch_date)
    return await screen(client, sheet, batch_date)

async def screen(client, sheet, batch_date=None):
    assessed = datetime.now(timezone.utc)
    batch_date = batch_date or london_batch_date()
    universe = await client.fetch_universe(); health(client)
    profiles, issues = await client.fetch_profiles(universe); health(client)
    audit, eligible = {}, []
    def record(symbol, status, why): audit[symbol] = [symbol, status, why, 'FMP']
    for item in universe:
        symbol = item['symbol']; profile = profiles.get(symbol)
        if profile is None: record(symbol, 'unresolved', 'Profile unavailable')
        elif profile.get('security_eligible') is not True:
            record(symbol, 'excluded_security' if profile.get('security_eligible') is False else 'unresolved', profile.get('security_reason', 'Eligibility unresolved'))
        else: eligible.append(symbol)
    incomes, more = await client.fetch_annual_income(eligible); issues.extend(more); health(client)
    candidates = []
    for symbol in eligible:
        history = incomes.get(symbol)
        if history is None or history.latest is None or history.error:
            record(symbol, 'unresolved', 'Latest completed annual income unavailable/conflicting'); continue
        try: require_statement_current(history.latest, 'Latest FY', assessed.date())
        except DataFreshnessError as error:
            record(symbol, 'unresolved', str(error)); continue
        margin = annual_margin(history.latest)
        if margin['status'] != 'ok': record(symbol, 'unresolved', reason(margin))
        elif not margin['passes']: record(symbol, 'rejected', reason(margin))
        else: candidates.append(symbol)
    verified, more = await client.verify_issuer_caps({s: profiles[s] for s in candidates})
    profiles.update(verified); issues.extend(more); health(client)
    currencies = {profiles[s].get('cap_currency') for s in candidates} | {incomes[s].latest.get('reportedCurrency') for s in candidates}
    currencies -= {None, ''}
    if candidates: currencies.add('USD')
    rates, more = await client.fetch_fx(currencies) if currencies else ({}, [])
    issues.extend(more); health(client)
    revenue, survivors = {}, []
    for symbol in candidates:
        result = assess_revenue(profiles[symbol], incomes[symbol].latest, rates); revenue[symbol] = result
        if result['status'] != 'ok': record(symbol, 'unresolved', reason(result))
        elif not result['passes']: record(symbol, 'rejected', reason(result))
        else: survivors.append(symbol)
    balances, more = await client.fetch_balances(survivors); issues.extend(more); health(client)
    rows, calculations = [HEADERS], {}
    for symbol in survivors:
        balance = balances.get(symbol)
        if balance is None: record(symbol, 'unresolved', 'Latest annual/interim balance unavailable/conflicting'); continue
        try: require_statement_current(balance, 'Latest balance', assessed.date())
        except DataFreshnessError as error:
            record(symbol, 'unresolved', str(error)); continue
        cash = net_cash(balance)
        if cash['status'] != 'ok': record(symbol, 'unresolved', reason(cash)); continue
        if not cash['passes']: record(symbol, 'rejected', 'Cash and short-term investments do not exceed total debt'); continue
        p, income, r = profiles[symbol], incomes[symbol].latest, revenue[symbol]
        calculations[symbol] = {'revenue': r, 'net_cash': cash}
        rows.append([symbol, p.get('companyName'), p.get('country'), p.get('exchangeShortName',p.get('exchange')),
            r.get('market_cap_to_revenue'), r.get('gross_margin'), cash.get('net_cash'), r.get('market_cap_reporting'),
            income.get('revenue'), income.get('grossProfit'), income.get('reportedCurrency'), income.get('date'), filing(income),
            cash.get('cash_and_short_term_investments'), cash.get('total_debt'), balance.get('reportedCurrency'), balance.get('date'), filing(balance),
            p.get('cap_amount'), p.get('cap_currency'), p.get('cap_currency_basis'), p.get('as_of'), r.get('fx_rate'), r.get('fx_as_of'),
            cash.get('cash_source'), p.get('security_identity','SYMBOL:'+symbol), '', assessed.isoformat(),
            'https://site.financialmodelingprep.com/financial-summary/'+symbol, 'FMP provider evidence; not independent issuer audit'])
        record(symbol, 'match', 'Market cap <USD100000000; Cap/revenue <=0.10; FY gross margin >=0.30; net cash >0')
    unique, identity_col = {}, HEADERS.index('Security identity')
    for row in sorted(rows[1:], key=lambda r: (str(r[2]), r[0])):
        if row[identity_col] in unique: record(row[0], 'duplicate_listing', 'Verified same security as '+unique[row[identity_col]][0])
        else: unique[row[identity_col]] = row
    rows = [HEADERS] + list(unique.values())
    issuers = {}
    for row in rows[1:]:
        p = profiles[row[0]]; key = str(p.get('cik') or '').lstrip('0') or str(p.get('companyName','')).casefold()+':'+str(p.get('country',''))
        issuers.setdefault(key, []).append(row)
    for group in issuers.values():
        if len(group)>1:
            for row in group: row[HEADERS.index('Possible issuer aliases')] = ', '.join(r[0] for r in group if r is not row)
    if set(audit) != {i['symbol'] for i in universe}: raise ValueError('Universe accounting mismatch')
    if not universe or not profiles or getattr(client,'health',{}).get('systemic_failure'): raise ValueError('Systemic provider failure')
    issues.extend(client.diagnostics)
    counts = Counter(r[1] for r in audit.values())
    coverage = {'status':'complete_with_unresolved' if counts['unresolved'] or issues else 'complete',
        'provider_universe':len(audit), **dict(counts), 'matched_rows':len(rows)-1,
        'assessment_utc':assessed.isoformat(), 'data_issue_count':len(issues), 'universe_sources':client.universe_source,
        'request_metrics':text(getattr(client,'metrics',{}))}
    audit_rows = [['Ticker','Status','Reason','Source']] + sorted(audit.values())
    audit_rows += [[i.symbol,'data_issue',i.stage+': '+i.reason,'FMP'] for i in issues]
    records = [['Ticker','Record','Provider data / calculation JSON','Retrieved UTC']]
    for row in rows[1:]:
        symbol = row[0]
        for kind, value in [('Profile',profiles[symbol]),('Latest annual income',incomes[symbol].latest),('Latest balance sheet',balances[symbol]),('Screen calculations',calculations[symbol])]:
            records.append([symbol,kind,text(value),assessed.isoformat()])
    for currency, rate in rates.items(): records.append([currency,'FX',text(rate.to_dict()),rate.fetched_at])
    health(client)
    return publish(sheet,batch_date,rows,audit_rows,coverage,CONFIG,records)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-account',required=True); parser.add_argument('--spreadsheet-id',required=True)
    args=parser.parse_args()
    try: asyncio.run(run(args.service_account,args.spreadsheet_id))
    except Exception as error:
        print('Revenue/net-cash screen failed: '+type(error).__name__,file=sys.stderr); return 1
    print('Private revenue/net-cash batch verified'); return 0
if __name__=='__main__': raise SystemExit(main())

