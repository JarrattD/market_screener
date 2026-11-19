"""Pure 0–7 ranking used by the UK & Channel Islands screen.

Ported from the existing Ranked dashboard formulas. Values in this module are
per whole issuer, not per share, and all monetary inputs must use one currency.
Missing inputs remain missing; they do not award a point.
"""

from math import isfinite
from statistics import mean

RATIO_KEYS = (
    "p_ncav", "p_tbv", "p_fcf_5y", "ev_fcf_5y",
    "p_fcf_latest", "ev_fcf_latest",
)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if isfinite(n) else None


def _deduct_preferred(amount, treatment):
    if amount is None or amount < 0:
        return None
    if amount == 0 or treatment == "already_included":
        return 0.0
    if treatment == "deduct_raw":
        return amount
    return None


def _book_ratio(cap, book, *, tangible=False):
    if cap is None or book is None:
        return None, "Grey", "Unavailable"
    if book == 0:
        return None, "Grey", "TBV zero" if tangible else "NCAV zero"
    ratio = cap / book
    if ratio < 0:
        return ratio, "Red" if tangible else "Grey", "TBV negative" if tangible else "NCAV negative"
    if ratio < 1:
        return ratio, "Green", ""
    if not tangible or ratio <= 1.5:
        return ratio, "Orange", ""
    return ratio, "Grey", ""


def _fcf_ratio(numerator, fcf, *, enterprise=False):
    if numerator is None or fcf is None:
        return None, "Grey", "Unavailable"
    ev_negative = enterprise and numerator < 0
    if fcf <= 0:
        label = "FCF negative" if fcf < 0 else "FCF zero"
        return None, "Grey", label + ("; EV negative" if ev_negative else "")
    ratio = numerator / fcf
    if ratio <= 5:
        return ratio, "Green", "EV negative" if ev_negative else ""
    return ratio, "Orange" if ratio <= 8 else "Grey", ""


def _ownership(owner, fallback, green_eligible):
    if owner is not None and 0 < owner <= 100:
        kind, band = "Owner", ("Green" if owner <= 20 else "Yellow" if owner < 50 else "Grey")
    elif fallback is not None and 0 <= fallback <= 100:
        kind, band = "FF", ("Green" if fallback >= 80 else "Yellow" if fallback > 50 else "Grey")
    else:
        return "Not found", "Grey", 0
    if not green_eligible and band == "Green":
        band = "Grey"
    return kind, band, int(band == "Green")


def score_company(
    *,
    market_cap=None, current_assets=None, total_liabilities=None,
    minority_interest=None, preferred_equity=None, total_equity=None,
    goodwill_and_intangibles=None, total_debt=None,
    cash_and_short_term_investments=None, annual_fcf=(), ownership_pct=None,
    preferred_ncav_treatment=None, preferred_tbv_treatment=None,
    equity_includes_minority=False, free_float_pct=None,
    ownership_green_eligible=True, market_status=None,
):
    """Score six ratios and one verified ownership signal.

    Supply the latest five consecutive completed financial years as latest-first
    `annual_fcf`, leaving unavailable years as None. The latest year can score
    independently of the five-year average. The supplied cap must have passed
    market-data verification; reporting/quote currency or share class ambiguity
    must not silently become a numerical price or EV multiple.

    A nonzero preferred book amount needs separate verified facts on whether
    NCAV and TBV should deduct it (deduct_raw) or not (already_included). The
    caller owns matching that evidence to the balance-sheet date and facts.
    Equity means parent shareholders' equity (excludes minorities) unless
    equity_includes_minority is True. Ownership percentages use 20, not 0.20.
    Only supply free_float_pct when use of an independently supported fallback
    is explicitly permitted under the established ranking method.
    """
    cap, assets, liabilities, minority, pref, equity, intangibles, debt, cash = map(
        _number, (market_cap, current_assets, total_liabilities,
                  minority_interest, preferred_equity, total_equity,
                  goodwill_and_intangibles, total_debt, cash_and_short_term_investments)
    )
    if cap is not None and cap <= 0:
        cap = None
    if any(n is not None and n < 0 for n in (assets, liabilities, intangibles, debt, cash)):
        assets, liabilities, intangibles, debt, cash = (
            n if n is not None and n >= 0 else None
            for n in (assets, liabilities, intangibles, debt, cash)
        )
    n_pref = _deduct_preferred(pref, preferred_ncav_treatment)
    t_pref = _deduct_preferred(pref, preferred_tbv_treatment)
    ncav = (assets - liabilities - minority - n_pref
            if None not in (assets, liabilities, minority, n_pref) else None)
    adjustment = minority if equity_includes_minority else 0.0
    tbv = (equity - adjustment - intangibles - t_pref
           if None not in (equity, adjustment, intangibles, t_pref) else None)
    ev = cap + debt - cash if None not in (cap, debt, cash) else None
    years = [_number(fcf) for fcf in list(annual_fcf or ())[:5]]
    latest = years[0] if years else None
    average = mean(years) if len(years) == 5 and None not in years else None
    results = [
        _book_ratio(cap, ncav),
        _book_ratio(cap, tbv, tangible=True),
        _fcf_ratio(cap, average),
        _fcf_ratio(ev, average, enterprise=True),
        _fcf_ratio(cap, latest),
        _fcf_ratio(ev, latest, enterprise=True),
    ]
    ownership_pct, free_float_pct = map(_number, (ownership_pct, free_float_pct))
    ownership_status, ownership_band, ownership_point = _ownership(
        ownership_pct, free_float_pct, ownership_green_eligible
    )
    if market_status in {"Inactive", "Check"}:
        results = [(None, "Grey", market_status)] * len(RATIO_KEYS)
        ownership_status, ownership_band, ownership_point = market_status, "Grey", 0
    ratio_values = dict(zip(RATIO_KEYS, (result[0] for result in results)))
    ratio_bands = dict(zip(RATIO_KEYS, (result[1] for result in results)))
    ratio_notes = dict(zip(RATIO_KEYS, (result[2] for result in results)))
    financial_points = sum(band == "Green" for band in ratio_bands.values())
    return {
        "score": financial_points + ownership_point,
        "financial_points": financial_points,
        "ownership_point": ownership_point,
        "ownership_status": ownership_status,
        "ownership_band": ownership_band,
        "ratios": ratio_values,
        "ratio_bands": ratio_bands,
        "ratio_notes": ratio_notes,
        "ncav": ncav,
        "tbv": tbv,
        "ev": ev,
        "mean_fcf": average,
        "latest_fcf": latest,
    }
