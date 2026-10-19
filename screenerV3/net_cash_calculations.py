"""Pure, auditable arithmetic for the separate acquisition screen.

No historical screen criteria, previously-seen exclusions or FCF hurdles are used.
Missing data is unresolved, never silently zero. Currency rates are GBP per unit.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date
from typing import Mapping, Optional


class MissingData(ValueError):
    """A required input is missing or ambiguous, rather than a failed criterion."""


def number(value, label):
    if value is None or isinstance(value, bool) or value == "":
        raise MissingData(f"Missing {label}")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise MissingData(f"Invalid {label}") from exc
    if not result.is_finite():
        raise MissingData(f"Nonfinite {label}")
    return result


def combined_cash(balance):
    value = balance.get("cashAndShortTermInvestments")
    if value is not None:
        cash = number(value, "cash and short-term investments")
    else:
        cash = number(balance.get("cashAndCashEquivalents"), "cash") + number(
            balance.get("shortTermInvestments"), "short-term investments")
    if cash < 0:
        raise MissingData("Negative cash field")
    return cash


def tangible_equity(balance):
    # FMP totalStockholdersEquity excludes noncontrolling interest, unlike totalEquity.
    equity = number(balance.get("totalStockholdersEquity"), "attributable equity")
    combined = balance.get("goodwillAndIntangibleAssets")
    if combined is None:
        combined = number(balance.get("goodwill"), "goodwill") + number(
            balance.get("intangibleAssets"), "other intangible assets")
    else:
        combined = number(combined, "goodwill and intangibles")
    if combined < 0:
        raise MissingData("Negative intangible assets")
    preferred = balance.get("preferredStock")
    if preferred is not None:
        preferred = number(preferred, "preferred equity")
        if preferred != 0:
            raise MissingData("Preferred equity requires manual attributable-equity check")
    return equity - combined


def gross_margin(income):
    if income.get("period") not in ("FY", "annual"):
        raise MissingData("Gross margin must use a completed annual statement")
    revenue = number(income.get("revenue"), "annual revenue")
    if revenue <= 0:
        raise MissingData("Annual gross-margin denominator is nonpositive")
    gross_profit = number(income.get("grossProfit"), "annual gross profit")
    # Loss-making gross profit remains a valid failed screen, not missing data.
    if gross_profit > revenue:
        raise MissingData("Gross profit exceeds revenue; verify accounting classification")
    return gross_profit / revenue


def market_cap_gbp(value, currency, fx):
    """Require independently identified capitalization currency, not price currency.

    Price in GBp/GBX does not establish capitalization units. Caller must resolve
    that distinction before passing currency. Unknown capitalization units fail.
    """
    cap = number(value, "market capitalisation")
    if cap <= 0:
        raise MissingData("Market capitalisation is nonpositive")
    if not currency or currency in ("GBp", "GBX", "ZAc", "ILA"):
        raise MissingData("Market-cap currency units require validation")
    rate = Decimal(1) if currency == "GBP" else number(fx.get(currency), "dated GBP FX rate")
    if rate <= 0:
        raise MissingData("FX rate is nonpositive")
    return cap * rate


@dataclass(frozen=True)
class Criteria:
    cap_gbp: Decimal = Decimal("75000000")
    minimum_gross_margin: Decimal = Decimal("0.30")
    # None means no extra hurdle. Configuration must explicitly identify selected rule.
    maximum_price_tbv: Optional[Decimal] = None
    cheapness_any: bool = False


def five_year_fcf(cashflows):
    if not cashflows or len(cashflows) < 5:
        raise MissingData("Five complete annual FCF periods unavailable")
    years = sorted(cashflows, key=lambda item: item.get("date", ""), reverse=True)[:5]
    dates = []
    currencies = set()
    values = []
    for item in years:
        if item.get("period") not in ("FY", "annual"):
            raise MissingData("FCF period is not annual")
        try:
            end = date.fromisoformat(item["date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MissingData("Invalid FCF year end") from exc
        if item.get("startDate"):
            try:
                start = date.fromisoformat(item["startDate"])
            except (TypeError, ValueError) as exc:
                raise MissingData("Invalid fiscal period start") from exc
            if not 350 <= (end-start).days + 1 <= 380:
                raise MissingData("Short or extended transition fiscal year")
        dates.append(end)
        currencies.add(item.get("reportedCurrency"))
        values.append(number(item.get("freeCashFlow"), "annual reported FCF"))
    if len(set(dates)) != 5 or any(not 350 <= (a-b).days <= 380 for a,b in zip(dates, dates[1:])):
        raise MissingData("Five consecutive full financial years unavailable")
    if len(currencies) != 1 or None in currencies or "" in currencies:
        raise MissingData("FCF currency changed or is missing")
    return sum(values) / Decimal(5), currencies.pop(), [d.isoformat() for d in dates]


def cheapness(cap_gbp, balance, cashflows, fx, net_cash):
    routes = {"tbv": None, "p_fcf": None, "ev_fcf": None}
    out = {"tbv": None, "price_tbv": None, "five_year_fcf": None,
           "p_fcf": None, "ev_fcf": None, "ev_reporting": None, "fcf_years": []}
    unknown = []
    currency = balance.get("reportedCurrency")
    rate = Decimal(1) if currency == "GBP" else number(fx.get(currency), "reporting-currency FX")
    if rate <= 0:
        raise MissingData("Nonpositive reporting-currency FX")
    cap_reporting = cap_gbp / rate
    ev = cap_reporting - net_cash
    out.update(cap_reporting=cap_reporting, ev_reporting=ev)
    try:
        tbv = tangible_equity(balance)
        out["tbv"] = tbv
        routes["tbv"] = False if tbv <= 0 else cap_reporting <= tbv
        out["price_tbv"] = cap_reporting / tbv if tbv > 0 else None
    except MissingData as exc:
        unknown.append(str(exc))
    try:
        mean, fcf_currency, years = five_year_fcf(cashflows)
        if fcf_currency != currency:
            raise MissingData("FCF and balance-sheet reporting currencies differ")
        out.update(five_year_fcf=mean, fcf_years=years)
        if mean > 0:
            out.update(p_fcf=cap_reporting/mean, ev_fcf=ev/mean)
            routes.update(p_fcf=out["p_fcf"] <= 5, ev_fcf=out["ev_fcf"] <= 5)
        else:
            routes.update(p_fcf=False, ev_fcf=False)
    except MissingData as exc:
        unknown.append(str(exc))
    out.update(cheapness_routes=routes, cheapness_pass_count=sum(v is True for v in routes.values()),
               cheapness_unknown=unknown, negative_ev=ev < 0)
    return out


def evaluate(cap, cap_currency, balance, income, fx: Mapping, criteria=Criteria(), cashflows=None):
    converted_cap = market_cap_gbp(cap, cap_currency, fx)
    debt = number(balance.get("totalDebt"), "total debt")
    if debt < 0:
        raise MissingData("Negative total debt")
    # TotalDebt already contains reported lease debt: never add leases twice.
    cash = combined_cash(balance)
    net_cash = cash - debt
    margin = gross_margin(income)
    failed = []
    if converted_cap > criteria.cap_gbp:
        failed.append("market cap above limit")
    if net_cash <= 0:
        failed.append("not net cash")
    if margin < criteria.minimum_gross_margin:
        failed.append("gross margin below limit")
    out = {"market_cap_gbp": converted_cap, "cash_and_st_investments": cash,
           "total_debt": debt, "net_cash": net_cash, "annual_gross_margin": margin,
           "failed_criteria": failed}
    if criteria.maximum_price_tbv is not None:
        tbv = tangible_equity(balance)
        reporting_currency = balance.get("reportedCurrency")
        tbv_gbp = tbv * (Decimal(1) if reporting_currency == "GBP" else
                        number(fx.get(reporting_currency), "reporting-currency GBP FX rate"))
        if tbv <= 0:
            failed.append("nonpositive tangible equity")
            ratio = None
        else:
            ratio = converted_cap / tbv_gbp
            if ratio > criteria.maximum_price_tbv:
                failed.append("price/TBV above limit")
        out.update(tbv=tbv, price_tbv=ratio)
    if criteria.cheapness_any and not failed:
        value = cheapness(converted_cap, balance, cashflows, fx, net_cash)
        out.update(value)
        if not value["cheapness_pass_count"]:
            if any(v is None for v in value["cheapness_routes"].values()):
                raise MissingData("No known cheapness pass; other route unresolved: " + "; ".join(value["cheapness_unknown"]))
            failed.append("none of the three cheapness routes passes")
    out["matches"] = not failed
    return out
