"""Shared data-quality rules. Stale evidence is unresolved, never zero."""
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from math import isfinite

POLICY = {"quote_max_age_days": 7, "statement_max_age_calendar_months": 18,
          "missing_or_stale": "unresolved, not rejected", "version": 1}


class DataFreshnessError(ValueError):
    pass


def cutoff_months(today, months):
    year, month0 = divmod(today.year * 12 + today.month - 1 - months, 12)
    month = month0 + 1
    return date(year, month, min(today.day, monthrange(year, month)[1]))


def quote_timestamp(value):
    """Parse the provider's actual timestamp, never a retrieval timestamp."""
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
        if not isfinite(numeric) or numeric <= 0:
            return None
        if numeric > 100_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            return None


def require_quote_current(profile, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = quote_timestamp(profile.get("as_of"))
    if stamp is None:
        raise DataFreshnessError("Market cap quote date missing or invalid; retrieval date is not a price date")
    if stamp > now + timedelta(hours=1) or stamp < now - timedelta(days=POLICY["quote_max_age_days"]):
        raise DataFreshnessError("Market cap quote is stale or future-dated; current price requires verification")
    return profile


def require_statement_current(row, label, today=None):
    today = today or datetime.now(timezone.utc).date()
    try:
        period = date.fromisoformat(str(row.get("date") or "")[:10])
    except (ValueError, TypeError):
        raise DataFreshnessError(f"{label} period date missing or invalid") from None
    if period > today:
        raise DataFreshnessError(f"{label} period date is in the future")
    if period < cutoff_months(today, POLICY["statement_max_age_calendar_months"]):
        raise DataFreshnessError(f"{label} dated {period} is older than 18 calendar months; latest filing requires verification")
    return row
