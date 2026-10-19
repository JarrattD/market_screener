"""Pure, conservative arithmetic for the ordinary-book/high-gross-margin screen.

The only gates are positive valid market cap <= USD 75m, positive ordinary
attributable book with P/B <= 0.5, and positive annual revenue with GM >= 30%.
There is no net-cash, FCF, operating-margin or tangible-book gate here.

Inputs and outputs use Decimal, including numbers received as JSON strings.
Threshold comparisons use exact cross multiplication, never rounded ratios.
Ratios for display use at least 50 significant digits. Missing is never zero.
Equity-identity checks allow at most one reporting-currency unit of absolute
rounding difference, disclose that difference, and never modify supplied book.
That reconciliation tolerance does not apply to any screening threshold.
The caller owns statement selection, completion/freshness checks and FX age.

FMP's official stable balance-sheet example (checked 2026-10-19) exposes
preferredStock, totalStockholdersEquity, totalEquity and minorityInterest:
https://site.financialmodelingprep.com/developer/docs/stable/balance-sheet-statement
Parent stockholders' equity excludes NCI but includes preferred stock. Thus
NCI is subtracted only when deriving parent equity from totalEquity. An absent
preferredStock is NOT evidence of zero; an explicit commonStockEquity field
is an ordinary-equity basis and needs no preferred-stock subtraction.

All public results have status='ok'|'missing', passes=bool|None and reasons.
Optional trend/asset diagnostics always have passes=None, never a screen gate.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Context, Decimal, InvalidOperation, localcontext
import re
from typing import Any


MARKET_CAP_LIMIT_USD = Decimal("75000000")
MAX_PRICE_TO_BOOK = Decimal("0.5")
MIN_GROSS_MARGIN = Decimal("0.30")
_ONE = Decimal(1)
_ZERO = Decimal(0)
_SUBUNITS = {
    "GBp": ("GBP", Decimal("0.01")),
    "GBX": ("GBP", Decimal("0.01")),
    "ZAc": ("ZAR", Decimal("0.01")),
    "ZAC": ("ZAR", Decimal("0.01")),
    "ILA": ("ILS", Decimal("0.01")),
    "ILa": ("ILS", Decimal("0.01")),
}


def _number(value: Any) -> Decimal | None:
    """Do not accept bool, blanks, NaN/infinity or unbounded provider exponents."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or abs(result.as_tuple().exponent) > 1000:
        return None
    return result


def _precision(*values: Decimal) -> int:
    return max(50, sum(len(v.as_tuple().digits) + abs(v.as_tuple().exponent)
                       for v in values) + 10)


def _product(*values: Decimal) -> Decimal:
    with localcontext(Context(prec=_precision(*values))):
        result = _ONE
        for value in values:
            result *= value
        return result


def _sum(*values: Decimal) -> Decimal:
    with localcontext(Context(prec=_precision(*values))):
        return sum(values, _ZERO)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext(Context(prec=_precision(numerator, denominator))):
        return numerator / denominator


def _currency(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip()
    if code in _SUBUNITS:
        return code
    code = code.upper()
    return code if re.fullmatch(r"[A-Z]{3}", code) else None


def _result(**fields: Any) -> dict[str, Any]:
    return {"status": "ok", "passes": None, "reasons": [], **fields}


def _missing(result: dict[str, Any], *reasons: str) -> dict[str, Any]:
    result.update(status="missing", passes=None)
    result["reasons"].extend(reasons)
    return result


def _get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def _fx_record(currency: str, fx_rates: Mapping) -> Any:
    if currency in fx_rates:
        return fx_rates[currency]
    # Case normalization does not erase the GBp versus GBP distinction.
    for key, record in fx_rates.items():
        if _currency(key) == currency:
            return record
    return None


def _gbp_rate(currency: str, fx_rates: Mapping) -> tuple[Decimal | None, dict, str | None]:
    base, scale = _SUBUNITS.get(currency, (currency, _ONE))
    if base == "GBP":
        return scale, {}, None  # Unit identity does not require a market quote.
    record = _fx_record(currency, fx_rates)
    source_currency = currency
    if record is None and scale != _ONE:
        record = _fx_record(base, fx_rates)
        source_currency = base
    rate = _number(_get(record, "rate_to_gbp"))
    stamp = _get(record, "as_of")
    if rate is None or rate <= 0:
        return None, {}, "missing_or_invalid_fx_" + currency
    if not isinstance(stamp, str) or not stamp.strip():
        return None, {}, "missing_fx_date_" + currency
    declared_currency = _get(record, "currency")
    if declared_currency is not None and _currency(declared_currency) != source_currency:
        return None, {}, "fx_currency_mismatch_" + currency
    if source_currency != currency:
        rate = _product(rate, scale)
    return rate, {source_currency: stamp}, None


def _convert(amount: Decimal, source: str, target: str, fx_rates: Mapping) -> dict:
    if source == target:
        return {"value": amount, "rate": _ONE, "numerator_rate": _ONE,
                "denominator_rate": _ONE, "as_of": {}, "reasons": []}
    source_rate, source_dates, source_error = _gbp_rate(source, fx_rates)
    target_rate, target_dates, target_error = _gbp_rate(target, fx_rates)
    reasons = [error for error in (source_error, target_error) if error]
    if reasons:
        return {"value": None, "rate": None, "numerator_rate": source_rate,
                "denominator_rate": target_rate, "as_of": {**source_dates, **target_dates},
                "reasons": reasons}
    return {"value": _ratio(_product(amount, source_rate), target_rate),
            "rate": _ratio(source_rate, target_rate), "numerator_rate": source_rate,
            "denominator_rate": target_rate, "as_of": {**source_dates, **target_dates},
            "reasons": []}


def _cap_input(profile: Mapping) -> dict:
    result = _result(market_cap=_number(profile.get("cap_amount")),
                     market_cap_currency=_currency(profile.get("cap_currency")),
                     cap_currency_basis=profile.get("cap_currency_basis"),
                     market_cap_usd=None, fx_as_of={})
    # Do not fall back to raw marketCap/mktCap or infer pence-cap units from price.
    if result["market_cap"] is None:
        return _missing(result, "missing_or_invalid_vetted_market_cap")
    if result["market_cap"] <= 0:
        return _missing(result, "nonpositive_market_cap")
    if (result["market_cap_currency"] is None
            or result["market_cap_currency"] in _SUBUNITS
            or profile.get("cap_currency_basis") == "unresolved"):
        return _missing(result, "unresolved_market_cap_currency")
    return result


def screen_market_cap(profile: Mapping, fx_rates: Mapping) -> dict:
    """Check a vetted positive market cap against the inclusive USD 75m limit."""
    result = _cap_input(profile)
    result["market_cap_limit_usd"] = MARKET_CAP_LIMIT_USD
    result["cap_to_usd_fx_rate"] = None
    if result["status"] == "missing":
        return result
    conversion = _convert(result["market_cap"], result["market_cap_currency"], "USD", fx_rates)
    result.update(market_cap_usd=conversion["value"], fx_as_of=conversion["as_of"],
                  cap_to_usd_fx_rate=conversion["rate"])
    if conversion["reasons"]:
        return _missing(result, *conversion["reasons"])
    result["passes"] = (_product(result["market_cap"], conversion["numerator_rate"])
                         <= _product(MARKET_CAP_LIMIT_USD, conversion["denominator_rate"]))
    if not result["passes"]:
        result["reasons"].append("market_cap_above_usd_75000000")
    return result


def _ordinary_equity(balance: Mapping) -> dict:
    result = _result(book_equity=None, equity_basis=None, preferred_stock=None,
                     preferred_stock_source=None, noncontrolling_interest=None,
                     noncontrolling_interest_source=None, parent_equity=None,
                     total_equity=None, equity_identity_checked=False,
                     equity_reconciliation_difference=None, common_equity_reconciliation_difference=None)
    present = lambda name: name in balance and balance[name] not in (None, "")
    parent = _number(balance.get("totalStockholdersEquity"))
    total = _number(balance.get("totalEquity"))
    nci_key = next((key for key in ("minorityInterest", "noncontrollingInterest")
                    if present(key)), None)
    nci = _number(balance.get(nci_key)) if nci_key else None
    preferred = _number(balance.get("preferredStock"))
    result.update(parent_equity=parent, total_equity=total, preferred_stock=preferred,
                  preferred_stock_source="preferredStock" if preferred is not None else None,
                  noncontrolling_interest=nci, noncontrolling_interest_source=nci_key)
    for key, parsed in (("totalStockholdersEquity", parent), ("totalEquity", total),
                        (nci_key, nci)):
        if key and present(key) and parsed is None:
            return _missing(result, "invalid_" + key)
    # Two populated NCI fields cannot disagree; negative NCI is possible and valid.
    other_nci = _number(balance.get("noncontrollingInterest"))
    if (present("minorityInterest") and present("noncontrollingInterest")
            and (other_nci is None or other_nci != nci)):
        return _missing(result, "conflicting_noncontrolling_interest")
    if parent is not None and total is not None:
        if nci is not None:
            difference = _sum(parent, nci, total.copy_negate())
            result["equity_reconciliation_difference"] = difference
            if difference.copy_abs() > _ONE:
                return _missing(result, "inconsistent_parent_total_equity_identity")
            result["equity_identity_checked"] = True
        elif parent != total:
            return _missing(result, "missing_nci_for_equity_reconciliation")
    if present("commonStockEquity"):
        common = _number(balance.get("commonStockEquity"))
        if common is None:
            return _missing(result, "invalid_commonStockEquity")
        validation_parent = parent
        if validation_parent is None and total is not None and nci is not None:
            validation_parent = _sum(total, nci.copy_negate())
            result["parent_equity"] = validation_parent
        if validation_parent is not None and preferred is not None:
            difference = _sum(common, preferred, validation_parent.copy_negate())
            result["common_equity_reconciliation_difference"] = difference
            if preferred < 0 or difference.copy_abs() > _ONE:
                return _missing(result, "inconsistent_common_parent_equity_identity")
        result.update(book_equity=common, equity_basis="commonStockEquity")
        return result
    if parent is None:
        if total is None:
            return _missing(result, "missing_attributable_book_equity")
        if nci is None:
            return _missing(result, "missing_noncontrolling_interest")
        parent = _sum(total, nci.copy_negate())
        result["parent_equity"] = parent
        basis = f"totalEquity - {nci_key} - preferredStock"
    else:
        basis = "totalStockholdersEquity - preferredStock"
    if preferred is None:
        return _missing(result, "missing_or_invalid_preferred_stock_adjustment")
    if preferred < 0:
        return _missing(result, "negative_preferred_stock")
    result.update(book_equity=_sum(parent, preferred.copy_negate()), equity_basis=basis)
    return result


def screen_book_value(profile: Mapping, balance: Mapping, fx_rates: Mapping) -> dict:
    """Ordinary attributable BOOK equity, not tangible book; inclusive P/B <= .5.

    USD amounts are optional display conversions for this gate. Its ratio only
    needs cap-to-reporting-currency FX; screen_market_cap separately needs USD.
    """
    result = _ordinary_equity(balance)
    result.update(book_currency=_currency(balance.get("reportedCurrency")),
                  book_equity_usd=None, price_to_book=None, market_cap_in_book_currency=None,
                  cap_to_book_fx_rate=None, cap_to_book_fx_as_of={}, book_to_usd_fx_rate=None,
                  book_to_usd_fx_as_of={}, max_price_to_book=MAX_PRICE_TO_BOOK,
                  balance_date=balance.get("date"), warnings=[])
    cap = _cap_input(profile)
    result.update(market_cap=cap["market_cap"], market_cap_currency=cap["market_cap_currency"])
    if result["status"] == "missing":
        return result
    if result["book_equity"] <= 0:
        result.update(passes=False)
        result["reasons"].append("nonpositive_ordinary_book_equity")
        return result
    if cap["status"] == "missing":
        return _missing(result, *cap["reasons"])
    if result["book_currency"] is None:
        return _missing(result, "missing_or_invalid_book_currency")
    conversion = _convert(cap["market_cap"], cap["market_cap_currency"], result["book_currency"], fx_rates)
    result.update(market_cap_in_book_currency=conversion["value"],
                  cap_to_book_fx_rate=conversion["rate"], cap_to_book_fx_as_of=conversion["as_of"])
    if conversion["reasons"]:
        return _missing(result, *conversion["reasons"])
    numerator = _product(cap["market_cap"], conversion["numerator_rate"])
    denominator = _product(result["book_equity"], conversion["denominator_rate"])
    result["price_to_book"] = _ratio(numerator, denominator)
    result["passes"] = numerator <= _product(MAX_PRICE_TO_BOOK, denominator)
    if not result["passes"]:
        result["reasons"].append("price_to_book_above_0_5")
    usd = _convert(result["book_equity"], result["book_currency"], "USD", fx_rates)
    result.update(book_equity_usd=usd["value"], book_to_usd_fx_rate=usd["rate"],
                  book_to_usd_fx_as_of=usd["as_of"], warnings=usd["reasons"])
    return result


def _fiscal_year(row: Mapping) -> int | None:
    value = row.get("fiscalYear")
    if value in (None, ""):
        value = row.get("calendarYear")
    value = str(value or "")
    return int(value) if re.fullmatch(r"[12][0-9]{3}", value) else None


def annual_margin(income: Mapping) -> dict:
    """Annual GP/revenue, never an EBIT-derived or provider-ratio substitute."""
    revenue = _number(income.get("revenue"))
    gross = _number(income.get("grossProfit"))
    operating = _number(income.get("operatingIncome"))
    result = _result(revenue=revenue, gross_profit=gross, gross_margin=None,
                     operating_income=operating, operating_margin=None,
                     ebit=_number(income.get("ebit")), fiscal_year=_fiscal_year(income),
                     date=income.get("date"), income_currency=_currency(income.get("reportedCurrency")),
                     minimum_gross_margin=MIN_GROSS_MARGIN)
    if str(income.get("period", "")).strip().upper() not in ("FY", "ANNUAL"):
        return _missing(result, "not_explicit_full_year_income")
    if revenue is None:
        return _missing(result, "missing_or_invalid_revenue")
    if revenue <= 0:
        result.update(passes=False)
        result["reasons"].append("nonpositive_annual_revenue")
        return result
    if operating is not None:
        result["operating_margin"] = _ratio(operating, revenue)
    if gross is None:
        return _missing(result, "missing_or_invalid_gross_profit")
    result["gross_margin"] = _ratio(gross, revenue)
    result["passes"] = gross >= _product(MIN_GROSS_MARGIN, revenue)
    if not result["passes"]:
        result["reasons"].append("annual_gross_margin_below_0_30")
    return result


def five_year_margins(rows: list[Mapping]) -> dict:
    """Up to five unique FYs, newest first; gaps and short histories are allowed.

    Caller selects restatements. If unreconciled conflicting duplicates reach
    this pure layer, report that year as missing rather than choose a good row.
    Identical duplicates do not count as additional years.
    """
    grouped: dict[int, list[dict]] = {}
    reasons = []
    for row in rows:
        if str(row.get("period", "")).strip().upper() not in ("FY", "ANNUAL"):
            continue
        year = _fiscal_year(row)
        if year is None:
            reasons.append("trend_row_missing_fiscal_year")
            continue
        grouped.setdefault(year, []).append(annual_margin(row))
    years = []
    for year in sorted(grouped, reverse=True)[:5]:
        candidates = grouped[year]
        chosen = dict(candidates[0])
        if any(candidate != candidates[0] for candidate in candidates[1:]):
            chosen = _result(status="missing", fiscal_year=year, date=None,
                             revenue=None, gross_profit=None, gross_margin=None,
                             operating_income=None, operating_margin=None, ebit=None,
                             income_currency=None, reasons=["unreconciled_duplicate_fiscal_year"])
        chosen["passes"] = None  # Optional context must never become a second margin gate.
        years.append(chosen)
    available = sum(year["gross_margin"] is not None for year in years)
    if not years:
        reasons.append("no_annual_margin_history")
    return _result(status="ok" if available else "missing", years=years,
                   available_years=available, reported_years=len(years), reasons=reasons,
                   complete_five_year_history=(available == 5 and
                       years[0]["fiscal_year"] - years[-1]["fiscal_year"] == 4))


def asset_diagnostics(balance: Mapping) -> dict:
    """Optional asset composition, reported currency; no missing-value imputation.

    Categories overlap (e.g. combined cash and investments), so are not a sum
    of mutually exclusive buckets. An explicit aggregate can be shown even if
    a component is missing. A derived sum requires every component.
    """
    field_map = {
        "cash": "cashAndCashEquivalents", "short_term_investments": "shortTermInvestments",
        "cash_and_short_term_investments": "cashAndShortTermInvestments",
        "receivables": "netReceivables", "inventory": "inventory",
        "goodwill": "goodwill", "intangibles": "intangibleAssets",
        "goodwill_and_intangibles": "goodwillAndIntangibleAssets",
        "property_plant_equipment": "propertyPlantEquipmentNet",
        "other_current_assets": "otherCurrentAssets",
        "other_noncurrent_assets": "otherNonCurrentAssets", "other_assets": "otherAssets",
        "total_current_assets": "totalCurrentAssets",
        "total_noncurrent_assets": "totalNonCurrentAssets", "total_assets": "totalAssets",
        "total_liabilities": "totalLiabilities",
    }
    result = _result(currency=_currency(balance.get("reportedCurrency")),
                     date=balance.get("date"), sources={}, ratios={})
    for name, source in field_map.items():
        result[name] = _number(balance.get(source))
        result["sources"][name] = source if result[name] is not None else None
    for combined, left, right in (
        ("cash_and_short_term_investments", "cash", "short_term_investments"),
        ("goodwill_and_intangibles", "goodwill", "intangibles"),
    ):
        if result[combined] is None and result[left] is not None and result[right] is not None:
            result[combined] = _sum(result[left], result[right])
            result["sources"][combined] = field_map[left] + " + " + field_map[right]
    total = result["total_assets"]
    for name in field_map:
        if name != "total_assets":
            share = _ratio(result[name], total) if total is not None and total > 0 and result[name] is not None else None
            result["ratios"][name + "_to_total_assets"] = share
            result[name + "_to_total_assets"] = share
    result["missing_fields"] = [name for name in field_map if result[name] is None]
    if total is None or total <= 0:
        result["reasons"].append("missing_or_nonpositive_total_assets_for_ratios")
    if all(result[name] is None for name in field_map):
        result.update(status="missing")
        result["reasons"].append("no_asset_diagnostics")
    return result
