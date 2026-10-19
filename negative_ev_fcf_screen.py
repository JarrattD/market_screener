"""Independent global negative-EV and positive cumulative-five-year-FCF screen.

The original screeners and the separate net-cash/high-margin screen are not
imported or modified. Running this CLI writes only its explicit new destination.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
import sys
import time
from zoneinfo import ZoneInfo

from screenerV3.data_freshness import POLICY, DataFreshnessError, require_quote_current, require_statement_current
from screenerV3.negative_ev_calculations import enterprise_value, cumulative_fcf
from screenerV3.negative_ev_output import open_destination, publish, resume_complete


CONFIG = {
    "version": "negative-ev-fcf-v2",
    "data_freshness": POLICY,
    "enterprise_value": "market_cap_in_reporting_currency + total_debt - cash_and_short_term_investments",
    "ev_strictly_below": "0",
    "cumulative_reported_fcf_strictly_above": "0",
    "fcf_window": "latest verified nonoverlapping five years; otherwise last five completed fiscal years",
    "include_negative_periods": True,
    "missing_data": "unresolved, never zero",
    "market_cap_ceiling": None,
    "gross_margin_filter": None,
    "fcf_ratio_filter": None,
}

HEADERS = [
    "Ticker", "Company", "Country", "Exchange", "Screening EV", "EV currency",
    "Market cap original", "Market cap currency", "Market cap in EV currency",
    "Total debt", "Cash + short-term investments", "Balance-sheet date", "Cap as of",
    "FX reporting currency per cap currency", "FX as of", "Cumulative 5Y reported FCF",
    "FCF currency", "FCF window start", "FCF window end", "FCF method",
    "Exact calendar 60 months", "FCF included components", "FCF warnings / fallback reason",
    "Assessment UTC", "Source", "Qualifications", "Security identity", "Possible issuer aliases",
]


def require_provider_health(client):
    fatal = getattr(client, "_fatal", None)
    if fatal is not None:
        raise fatal


def json_text(value):
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def audit_row(symbol, status, reason="", source="FMP"):
    return [symbol, status, reason, source]


def reasons(result):
    value = result.get("reason_codes") or result.get("reasons") or result.get("reason") or []
    return "; ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)


def ev_fx(profile, balance, rates):
    cap_currency, reporting = profile.get("cap_currency"), balance.get("reportedCurrency")
    if cap_currency == reporting:
        return {}, Decimal(1), "identity"
    if cap_currency not in rates or reporting not in rates:
        return {}, None, ""
    cap_rate, reporting_rate = rates[cap_currency], rates[reporting]
    cross = Decimal(str(cap_rate.rate_to_gbp)) / Decimal(str(reporting_rate.rate_to_gbp))
    return {"from_currency": cap_currency, "to_currency": reporting, "rate": cross}, cross, min(
        cap_rate.as_of, reporting_rate.as_of)


async def run(service_account, spreadsheet_id, client=None):
    sheet = open_destination(service_account, spreadsheet_id)
    existing = resume_complete(sheet, datetime.now(ZoneInfo("Europe/London")).date(), CONFIG)
    if existing:
        print("Existing dated negative-EV results verified; no duplicate run", flush=True)
        return existing
    if client is None:
        from screenerV3.negative_ev_data import FMPClient
        async with FMPClient() as session:
            return await screen(session, sheet)
    return await screen(client, sheet)


async def screen(client, sheet):
    started = time.perf_counter()
    assessed = datetime.now(timezone.utc)
    batch_date = assessed.astimezone(ZoneInfo("Europe/London")).date()
    print("Loading fresh global universe and profiles", flush=True)
    universe = await client.fetch_universe()
    require_provider_health(client)
    profiles, issues = await client.fetch_profiles(universe)
    require_provider_health(client)
    audit, eligible = {}, []
    for item in universe:
        symbol = item["symbol"]
        profile = profiles.get(symbol)
        if profile is None:
            audit[symbol] = audit_row(symbol, "unresolved", "Profile unavailable")
        elif profile.get("security_eligible") is not True:
            audit[symbol] = audit_row(symbol,
                "excluded_security" if profile.get("security_eligible") is False else "unresolved",
                profile.get("security_reason", "Security eligibility unknown"))
        else:
            try:
                require_quote_current(profile, assessed)
            except DataFreshnessError as exc:
                audit[symbol] = audit_row(symbol, "unresolved", str(exc))
                continue
            eligible.append(symbol)
    print("Reading latest annual and interim balance sheets", flush=True)
    balances, balance_issues = await client.fetch_balances(eligible)
    require_provider_health(client)
    issues.extend(balance_issues)
    # Same-currency EV needs no external FX rate. Missing optional GBP display
    # data must never become an additional exclusion.
    fx_currencies = set()
    for symbol in eligible:
        b = balances.get(symbol)
        p = profiles[symbol]
        if b and b.get("reportedCurrency") != p.get("cap_currency"):
            fx_currencies.update([b.get("reportedCurrency"), p.get("cap_currency")])
    rates, fx_issues = await client.fetch_fx({c for c in fx_currencies if c})
    require_provider_health(client)
    issues.extend(fx_issues)
    values, survivors = {}, []
    for symbol in eligible:
        balance = balances.get(symbol)
        if not balance:
            audit[symbol] = audit_row(symbol, "unresolved", "Latest balance sheet unavailable or unverified")
            continue
        try:
            require_statement_current(balance, "Latest balance sheet", assessed.date())
        except DataFreshnessError as exc:
            audit[symbol] = audit_row(symbol, "unresolved", str(exc))
            continue
        profile = profiles[symbol]
        conversion, rate, rate_date = ev_fx(profile, balance, rates)
        value = enterprise_value(profile.get("cap_amount"), profile.get("cap_currency"), balance, conversion)
        if value.get("status") != "ok":
            audit[symbol] = audit_row(symbol, "unresolved", reasons(value))
        elif not value["passes"]:
            audit[symbol] = audit_row(symbol, "rejected", f"Screening EV {value['enterprise_value']} {value['currency']} is zero or positive")
        else:
            values[symbol] = (value, rate, rate_date)
            survivors.append(symbol)
    print("Reading five-year cash-flow history for negative-EV stocks", flush=True)
    histories, cashflow_issues = await client.fetch_cashflow_history(survivors)
    require_provider_health(client)
    issues.extend(cashflow_issues)
    rows, matched = [HEADERS], {}
    for symbol in survivors:
        history = histories.get(symbol)
        if history is None:
            audit[symbol] = audit_row(symbol, "unresolved", "Cash-flow history unavailable")
            continue
        result = cumulative_fcf(history.annual, history.interim, history.ttm,
                                symbol=symbol, as_of=assessed.date())
        if result.get("status") != "ok":
            audit[symbol] = audit_row(symbol, "unresolved", reasons(result))
            continue
        try:
            require_statement_current({"date": result.get("period_end")},
                                      "Five-year FCF window endpoint", assessed.date())
        except DataFreshnessError as exc:
            audit[symbol] = audit_row(symbol, "unresolved", str(exc))
            continue
        if not result["passes"]:
            audit[symbol] = audit_row(symbol, "rejected", f"Cumulative FCF {result['fcf_sum']} {result['currency']} is zero or negative; {result['method']}; {result['period_start']} to {result['period_end']}")
            continue
        p, b = profiles[symbol], balances[symbol]
        ev, rate, rate_date = values[symbol]
        debt = ev["total_debt"]
        cash = ev["cash_and_short_term_investments"]
        cap_reporting = ev["market_cap"]
        warnings = list(result.get("warnings") or [])
        warnings.extend(f"{key}: {value}" for key, value in sorted(history.errors.items()))
        rows.append([
            symbol, p.get("companyName", ""), p.get("country", ""),
            p.get("exchangeShortName", p.get("exchange", "")),
            ev["enterprise_value"], b.get("reportedCurrency", ""),
            p.get("cap_amount"), p.get("cap_currency"), cap_reporting, debt, cash,
            b.get("date", ""), p.get("as_of", ""), rate, rate_date,
            result["fcf_sum"], result["currency"], result["period_start"], result["period_end"],
            result["method"], result.get("exact_calendar_60_months"),
            json_text(result.get("components") or []), "; ".join(map(str, warnings)), assessed.isoformat(),
            "https://site.financialmodelingprep.com/financial-summary/" + symbol,
            "Reported-data screening EV, not acquisition proceeds or distributable cash. Verify restricted/customer cash, lease inclusion, preferred/minority claims and reporting changes. No extra valuation or quality filter.",
            p.get("security_identity", "SYMBOL:" + symbol), "",
        ])
        matched[symbol] = result
        audit[symbol] = audit_row(symbol, "match", "EV < 0 and cumulative five-year reported FCF > 0")
    unique = {}
    for row in sorted(rows[1:], key=lambda r: (r[2], r[0])):
        identity = row[26]
        if identity in unique:
            audit[row[0]] = audit_row(row[0], "duplicate_listing", "Verified same security as " + unique[identity][0])
        else:
            unique[identity] = row
    rows = [HEADERS] + list(unique.values())
    issuer_groups = {}
    for row in rows[1:]:
        p = profiles[row[0]]
        cik = str(p.get("cik") or "").lstrip("0")
        identity = "CIK:" + cik if cik else "NAME:" + str(p.get("companyName", "")).casefold().strip() + ":" + str(p.get("country", ""))
        issuer_groups.setdefault(identity, []).append(row)
    for group in issuer_groups.values():
        if len(group) > 1:
            for row in group:
                row[27] = "Possible issuer aliases: " + ", ".join(other[0] for other in group if other is not row)
    expected = {item["symbol"] for item in universe}
    if set(audit) != expected:
        raise ValueError("Universe coverage accounting mismatch")
    if not universe or not profiles or getattr(client, "health", {}).get("systemic_failure"):
        raise ValueError("Systemic provider failure; no completed result published")
    issues.extend(client.diagnostics)
    counts = Counter(row[1] for row in audit.values())
    methods = Counter(matched[row[0]]["method"] for row in rows[1:])
    status = "complete_with_unresolved" if counts["unresolved"] or issues else "complete"
    coverage = {
        "status": status, "provider_universe": len(expected), **dict(counts),
        "matched_rows": len(rows) - 1, "negative_ev_candidates": len(survivors),
        "fcf_methods": json_text(dict(methods)), "assessment_utc": assessed.isoformat(),
        "data_issue_count": len(issues), "universe_sources": client.universe_source,
        "data_stage_seconds": round(time.perf_counter() - started, 3),
        "request_metrics": json_text(getattr(client, "metrics", {})),
    }
    audit_rows = [["Ticker", "Status", "Reason", "Source"]] + sorted(audit.values())
    audit_rows.extend([[i.symbol, "data_issue", i.stage + ": " + i.reason, "FMP"] for i in issues])
    records = [["Ticker", "Record", "Provider data / calculation JSON", "Retrieved UTC"]]
    for row in rows[1:]:
        symbol, history = row[0], histories[row[0]]
        for kind, data in [("Profile", profiles[symbol]), ("Latest balance sheet", balances[symbol]),
                           ("Five-year calculation", matched[symbol])]:
            records.append([symbol, kind, json_text(data), history.fetched_at])
        for kind, observations in (("Annual cash flow", history.annual), ("Interim cash flow", history.interim),
                                   ("TTM cash flow", history.ttm)):
            for observation in observations:
                records.append([symbol, kind, json_text(observation), history.fetched_at])
    for currency, rate in rates.items():
        records.append([currency, "FX", json_text(rate.to_dict()), rate.fetched_at])
    require_provider_health(client)
    receipt = publish(sheet, batch_date, rows, audit_rows, coverage, CONFIG, records)
    print("New-screen private results and coverage verified", flush=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--spreadsheet-id", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.service_account, args.spreadsheet_id))
    except Exception as exc:
        print("Negative-EV screen failed before verified completion: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
