"""Exact entry arithmetic for the revenue-multiple / margin / net-cash screen.

Only three investment gates: cap <= 10% of positive FY revenue, FY GM >=30%,
and cash plus short-term investments > total debt. Selection of the newest
available statements, quote/FX freshness and issuer evidence acquisition belong
to the provider. No FCF, equity or absolute capitalization ceiling is imposed.
"""
from collections.abc import Mapping
from decimal import Decimal

from .book_margin_calculations import (
    _cap_input, _convert, _currency, _missing, _number, _product, _ratio,
    _result, _SUBUNITS, _sum, annual_margin,
)

MAX_CAP_TO_REVENUE = Decimal("0.10")
MIN_GROSS_MARGIN = Decimal("0.30")


def _issuer_evidence(profile, cap):
    """Require upstream issuer-scope proof bound to this exact vetted cap.

    Merely reconciling a listing price and its listed-class shares does not
    establish whole-issuer capitalization. The provider must establish class /
    ADR scope separately; this contract prevents a raw profile from bypassing it.
    """
    evidence = profile.get("whole_issuer_cap_evidence")
    return (profile.get("whole_issuer_cap_verified") is True
            and isinstance(evidence, Mapping)
            and evidence.get("validated") is True
            and evidence.get("scope") == "whole_issuer"
            and isinstance(evidence.get("source"), str)
            and bool(evidence["source"].strip())
            and _number(evidence.get("cap_amount")) == cap["market_cap"]
            and _currency(evidence.get("cap_currency")) == cap["market_cap_currency"])


def assess_revenue(profile, income, fx_rates):
    """Assess inclusive revenue-multiple and same-FY gross-margin gates.

    fx_rates follows the existing dated currency-to-GBP cross-rate contract.
    The caller validates quote and FX dates before invoking this pure function.
    Both same-currency and FX threshold comparisons are exact cross-products;
    displayed division is never used for the pass/fail decision.
    """
    result = _result(market_cap_to_revenue=None, market_cap_reporting=None,
                     revenue=None, gross_profit=None, gross_margin=None,
                     income_currency=None, maximum_cap_to_revenue=MAX_CAP_TO_REVENUE,
                     minimum_gross_margin=MIN_GROSS_MARGIN,
                     revenue_multiple_passes=None, gross_margin_passes=None,
                     cap_to_income_fx_rate=None, cap_to_income_fx_as_of={},
                     fx_rate=None, fx_as_of={})
    if not isinstance(profile, Mapping) or not isinstance(income, Mapping):
        return _missing(result, "missing_profile_or_annual_income")
    cap = _cap_input(profile)
    result.update(market_cap_original=cap.get("market_cap"),
                  market_cap_currency=cap.get("market_cap_currency"))
    if cap["status"] != "ok":
        return _missing(result, *cap["reasons"])
    if not _issuer_evidence(profile, cap):
        return _missing(result, "unverified_whole_issuer_market_cap")
    margin = annual_margin(income)
    result.update(revenue=margin["revenue"], gross_profit=margin["gross_profit"],
                  gross_margin=margin["gross_margin"],
                  income_currency=margin["income_currency"],
                  fiscal_year=margin["fiscal_year"], date=margin["date"],
                  gross_margin_passes=margin["passes"])
    if margin["status"] != "ok":
        return _missing(result, *margin["reasons"])
    if margin["revenue"] <= 0:
        result.update(passes=False, revenue_multiple_passes=False)
        result["reasons"].extend(margin["reasons"])
        return result
    target = margin["income_currency"]
    if target is None or target in _SUBUNITS:
        return _missing(result, "missing_or_invalid_income_reporting_currency")
    if not isinstance(fx_rates, Mapping):
        return _missing(result, "invalid_fx_mapping")
    conversion = _convert(cap["market_cap"], cap["market_cap_currency"], target, fx_rates)
    if conversion["reasons"]:
        return _missing(result, *conversion["reasons"])
    multiple_passes = (_product(cap["market_cap"], conversion["numerator_rate"])
                       <= _product(MAX_CAP_TO_REVENUE, margin["revenue"],
                                   conversion["denominator_rate"]))
    result.update(market_cap_reporting=conversion["value"],
                  market_cap_to_revenue=_ratio(conversion["value"], margin["revenue"]),
                  cap_to_income_fx_rate=conversion["rate"],
                  cap_to_income_fx_as_of=conversion["as_of"],
                  fx_rate=conversion["rate"], fx_as_of=conversion["as_of"],
                  revenue_multiple_passes=multiple_passes,
                  passes=multiple_passes and margin["passes"])
    if not multiple_passes:
        result["reasons"].append("market_cap_above_0_10_of_latest_annual_revenue")
    result["reasons"].extend(margin["reasons"])
    return result


def net_cash(balance):
    """Use combined cash once, or require both explicit separate components.

    Total debt includes whatever leases the provider included; no second lease
    addition is made. Explicit zero debt is valid. Contradictory supplied cash
    identities are unresolved (one reporting-unit reconciliation tolerance,
    never applied to the strict net-cash threshold).
    """
    result = _result(total_debt=None, total_debt_source="totalDebt", cash_and_short_term_investments=None,
                     net_cash=None, cash_source=None, balance_currency=None,
                     cash_reconciliation_difference=None)
    if not isinstance(balance, Mapping):
        return _missing(result, "missing_balance")
    currency = _currency(balance.get("reportedCurrency"))
    result.update(balance_currency=currency, date=balance.get("date"))
    if currency is None or currency in _SUBUNITS:
        return _missing(result, "missing_or_invalid_balance_reporting_currency")
    debt = _number(balance.get("totalDebt"))
    result["total_debt"] = debt
    if debt is None or debt < 0:
        return _missing(result, "missing_or_invalid_total_debt")
    combined = balance.get("cashAndShortTermInvestments")
    separate = ("cashAndCashEquivalents", "shortTermInvestments")
    components = [_number(balance.get(field)) for field in separate]
    if combined not in (None, ""):
        cash = _number(combined)
        source = "cashAndShortTermInvestments"
        if cash is None or cash < 0:
            return _missing(result, "invalid_combined_cash_and_short_term_investments")
        for field, value in zip(separate, components):
            if balance.get(field) not in (None, "") and (value is None or value < 0):
                return _missing(result, "invalid_" + field)
        if all(value is not None for value in components):
            difference = _sum(cash, *[value.copy_negate() for value in components])
            result["cash_reconciliation_difference"] = difference
            if abs(difference) > Decimal(1):
                return _missing(result, "conflicting_combined_and_separate_cash")
    else:
        for field, value in zip(separate, components):
            if value is None or value < 0:
                return _missing(result, "missing_or_invalid_" + field)
        cash = _sum(*components)
        source = "cashAndCashEquivalents + shortTermInvestments"
    amount = _sum(cash, debt.copy_negate())
    result.update(cash_and_short_term_investments=cash, cash_source=source,
                  net_cash=amount, passes=amount > 0)
    if amount <= 0:
        result["reasons"].append("cash_and_short_term_investments_not_above_total_debt")
    return result
