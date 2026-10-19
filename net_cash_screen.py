"""Separate small-cap, net-cash, high-gross-margin acquisition screen.

Only this program writes the new Sheet. Old screen universes and logic are untouched.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from screenerV3.net_cash_calculations import Criteria, MissingData, evaluate, market_cap_gbp
from screenerV3.net_cash_output import open_destination, publish, resume_complete

# User-approved inclusive OR gate, separate from the master watchlist rules.
CONFIG = {"market_cap_gbp_max": "75000000", "net_cash_strictly_positive": True,
          "annual_gross_margin_min": "0.30", "cheapness_rule": "P/TBV<=1 OR P/5YFCF<=5 OR EV/5YFCF<=5"}

HEADERS = ["Ticker", "Company", "Country", "Exchange", "Market cap GBP", "Market cap currency",
           "Market cap original", "Cash + short-term investments", "Total debt", "Net cash",
           "Reporting currency", "Latest annual gross margin", "Income year end", "Balance-sheet date",
           "FX GBP per cap currency", "FX as of", "Price/cap as of", "Assessment UTC",
           "Cap currency basis", "Financial sources", "Qualifications", "TBV", "P/TBV", "5Y FCF mean", "P/5Y FCF", "EV/5Y FCF", "EV reporting currency", "TBV route passes", "P/FCF route passes", "EV/FCF route passes", "Cheapness route count", "FCF fiscal year ends", "Issuer/listing identity", "Possible issuer aliases"]


def criteria():
    if CONFIG["cheapness_rule"] == "pending":
        raise ValueError("Cheapness criterion has not yet been approved")
    return Criteria(cheapness_any=True)


def audit_row(symbol, status, reason="", source=""):
    return [symbol, status, reason, source]


async def run(service_account, client=None):
    rule = criteria()  # Fail before API calls or writes until final user choice.
    sheet = open_destination(service_account)
    existing = resume_complete(sheet, datetime.now(ZoneInfo("Europe/London")).date(), CONFIG)
    if existing:
        print("Existing dated results verified; no duplicate run", flush=True)
        return existing
    from screenerV3.net_cash_data import FMPClient
    if client is None:
        async with FMPClient() as session:
            return await screen(session, sheet, rule)
    return await screen(client, sheet, rule)


async def screen(client, sheet, rule):
    assessed_at = datetime.now(timezone.utc).isoformat()
    batch_date = datetime.now(ZoneInfo("Europe/London")).date()
    print("Loading global provider universe and profiles", flush=True)
    universe = await client.fetch_universe()
    profiles, issues = await client.fetch_profiles(universe)
    rates, fx_issues = await client.fetch_fx({p.get("cap_currency") for p in profiles.values() if p.get("cap_currency")})
    issues.extend(fx_issues)
    fx = {key: value.rate_to_gbp for key, value in rates.items()}
    audit = {}
    candidates = []
    for item in universe:
        symbol = item["symbol"]
        p = profiles.get(symbol)
        if p is None:
            audit[symbol] = audit_row(symbol, "unresolved", "Profile unavailable")
            continue
        if p.get("security_eligible") is not True:
            audit[symbol] = audit_row(symbol, "excluded_security" if p.get("security_eligible") is False else "unresolved", p.get("security_reason", "Security eligibility unverified"))
            continue
        try:
            cap = market_cap_gbp(p.get("cap_amount"), p.get("cap_currency"), fx)
            if cap > rule.cap_gbp:
                audit[symbol] = audit_row(symbol, "rejected", "Market cap above £75m")
                continue
        except MissingData as exc:
            audit[symbol] = audit_row(symbol, "unresolved", str(exc))
            continue
        candidates.append(symbol)
    print("Reading annual and latest balance-sheet financials", flush=True)
    financials, financial_issues = await client.fetch_financials(candidates, include_cashflow=True)
    issues.extend(financial_issues)
    reporting_currencies = {f.balance.get("reportedCurrency") for f in financials.values() if f.balance}
    more_rates, more_issues = await client.fetch_fx(reporting_currencies - set(rates))
    rates.update(more_rates)
    fx.update({key: value.rate_to_gbp for key, value in more_rates.items()})
    issues.extend(more_issues)
    rows = [HEADERS]
    for symbol in candidates:
        p = profiles[symbol]
        f = financials.get(symbol)
        if f is None or not f.income or not f.balance:
            audit[symbol] = audit_row(symbol, "unresolved", "Required annual income or latest balance sheet unavailable")
            continue
        try:
            result = evaluate(p["cap_amount"], p["cap_currency"], f.balance, f.income, fx, rule, cashflows=f.cashflows if f.cashflow_years_complete else None)
        except MissingData as exc:
            audit[symbol] = audit_row(symbol, "unresolved", str(exc))
            continue
        if not result["matches"]:
            audit[symbol] = audit_row(symbol, "rejected", "; ".join(result["failed_criteria"]))
            continue
        rate = rates[p["cap_currency"]]
        rows.append([symbol, p.get("companyName", ""), p.get("country", ""), p.get("exchangeShortName", p.get("exchange", "")),
                     result["market_cap_gbp"], p["cap_currency"], p["cap_amount"], result["cash_and_st_investments"],
                     result["total_debt"], result["net_cash"], f.balance.get("reportedCurrency", ""),
                     result["annual_gross_margin"], f.income.get("date", ""), f.balance.get("date", ""),
                     rate.rate_to_gbp, rate.as_of, p.get("as_of", ""), assessed_at, p.get("cap_currency_basis", ""),
                     "https://site.financialmodelingprep.com/financial-summary/" + symbol,
                     "Initial reported data; verify restricted/customer cash, leases and acquisition constraints in reports. Annual FCF periods use provider FY classification and adjacent dates; oldest-year exact duration is inferred if its start and preceding year end are unavailable. " +
                     ("Negative EV is a net-cash screen result, not a claim cash is freely distributable. " if result["negative_ev"] else "") +
                     "; ".join(result["cheapness_unknown"]),
                     result["tbv"], result["price_tbv"], result["five_year_fcf"], result["p_fcf"], result["ev_fcf"], result["ev_reporting"],
                     result["cheapness_routes"]["tbv"], result["cheapness_routes"]["p_fcf"], result["cheapness_routes"]["ev_fcf"],
                     result["cheapness_pass_count"], ", ".join(result["fcf_years"]), p.get("security_identity", "SYMBOL:"+symbol), ""])
        audit[symbol] = audit_row(symbol, "match", "Meets configured screen")
    # Collapse verified same-security listing aliases after assessment; never infer
    # issuer identity from similar names. Different share classes/ADRs remain flagged
    # for issuer-level review rather than silently discarding a possible candidate.
    unique = {}
    for row in sorted(rows[1:], key=lambda r: (-r[30], r[4], r[0])):
        identity = row[32]
        if identity in unique:
            audit[row[0]] = audit_row(row[0], "duplicate_listing", "Same security as " + unique[identity][0])
        else:
            unique[identity] = row
    rows = [HEADERS] + list(unique.values())
    # Different instruments can belong to one issuer. Flag supported possible
    # aliases for research dedup, without claiming unlike securities equivalent.
    issuer_groups = {}
    for row in rows[1:]:
        p = profiles[row[0]]
        cik = str(p.get("cik") or "").lstrip("0")
        identity = "CIK:" + cik if cik else "NAME:" + str(p.get("companyName", "")).casefold().strip() + ":" + str(p.get("country", ""))
        issuer_groups.setdefault(identity, []).append(row)
    for group in issuer_groups.values():
        if len(group) > 1:
            for row in group:
                row[33] = "Review issuer duplicates before reporting: " + ", ".join(other[0] for other in group if other is not row)
    if set(audit) != {item["symbol"] for item in universe}:
        raise ValueError("Universe coverage accounting mismatch")
    issues.extend(client.diagnostics)
    counts = Counter(row[1] for row in audit.values())
    if not profiles or (candidates and not financials) or getattr(client, "health", {}).get("systemic_failure"):
        raise ValueError("Systemic provider failure; no complete result published")
    status = "complete_with_unresolved" if counts["unresolved"] or issues else "complete"
    coverage = {"status": status, "provider_universe": len(audit), **dict(counts), "matched_rows": len(rows)-1,
                "assessment_utc": assessed_at, "data_issue_count": len(issues),
                "universe_sources": client.universe_source}
    # Keep the complete assessment and API diagnostics private, not in public logs/artifacts.
    audit_rows = [["Ticker", "Status", "Reason", "Source"]] + sorted(audit.values())
    audit_rows += [[i.symbol, "data_issue", f"{i.stage}: {i.reason}", "FMP"] for i in issues]
    records = [["Ticker", "Record", "Provider data JSON", "Retrieved UTC"]]
    for row in rows[1:]:
        symbol = row[0]
        f = financials[symbol]
        for kind, data in [("Profile", profiles[symbol]), ("Annual income", f.income), ("Latest balance sheet", f.balance)]:
            records.append([symbol, kind, json.dumps(data, sort_keys=True), f.fetched_at])
        for data in f.cashflows:
            records.append([symbol, "Annual cash flow", json.dumps(data, sort_keys=True), f.fetched_at])
    for currency, rate in rates.items():
        records.append([currency, "FX", json.dumps(rate.to_dict(), sort_keys=True), rate.fetched_at])
    receipt = publish(sheet, batch_date, rows, audit_rows, coverage, CONFIG, records)
    print("New-screen private results and coverage verified", flush=True)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-account", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.service_account))
    except Exception as exc:
        # Library exceptions may include credential-bearing request URLs. Do not echo them.
        print("New screen failed before a verified completion: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
