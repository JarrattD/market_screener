"""Refresh every eligible listing before valuation gates, using fresh quotes.

Shared by independent screeners without importing their providers or criteria.
The client supplies its existing paced request method and exception classes.
"""
import asyncio
from datetime import datetime, timezone
from math import isfinite
from urllib.parse import quote

from .data_freshness import DataFreshnessError, quote_timestamp, require_quote_current


def _positive(value):
    if isinstance(value, bool):
        return False
    try:
        return isfinite(float(value)) and float(value) > 0
    except (ValueError, TypeError, OverflowError):
        return False


def _select(rows, symbol):
    matching = [r for r in rows if str(r.get("symbol") or "") == symbol]
    dated = [(quote_timestamp(r.get("timestamp")), r) for r in matching]
    dated = [(stamp, row) for stamp, row in dated if stamp is not None]
    if not dated:
        return None, "missing_timestamped_quote"
    newest = max(stamp for stamp, _ in dated)
    latest = [row for stamp, row in dated if stamp == newest]
    values = {(str(r.get("marketCap")), str(r.get("price")), str(r.get("currency"))) for r in latest}
    if len(values) > 1:
        return None, "conflicting_latest_quotes"
    return latest[0], None


async def refresh_quotes(client, profiles, *, normalize_profile, reconcile_cap_units,
                         issue_type, error_type, auth_error_type, now=None, batch_size=100):
    """Return copied profiles and issues; never retain profile caps on failure.

Successful but partial batches may be reconciled through the symbol endpoint.
Access denials never trigger an alternative route. Legacy quote enrichment is
only for current shares absent from a successful subunit quote, not to bypass
denial. A total quote outage marks the client's run-wide health as failed.
"""
    if not 1 <= batch_size <= 100:
        raise ValueError("quote batch_size must be between 1 and 100")
    now = now or datetime.now(timezone.utc)
    result = {s: dict(p) for s, p in profiles.items()}
    symbols = sorted(s for s, p in result.items() if p.get("security_eligible") is True)
    issues, quoted, missing = [], {}, []
    for symbol in symbols:
        result[symbol]["profile_market_cap_unverified"] = profiles[symbol].get("marketCap")
        result[symbol].update(cap_amount=None, cap_currency=None, as_of="",
                              marketCap=None, mktCap=None, price=None,
                              quote_source="", quote_evidence={},
                              cap_currency_basis="unresolved", quote_verified=False)

    def fail(symbol, reason):
        issues.append(issue_type(symbol, "quote_freshness", reason))

    def propagate(exc):
        fatal = getattr(client, "_fatal", None)
        if fatal is not None:
            raise fatal
        if isinstance(exc, auth_error_type):
            raise exc

    for start in range(0, len(symbols), batch_size):
        batch = symbols[start:start + batch_size]
        try:
            rows = await client._request("/stable/batch-quote", {"symbols": ",".join(batch)})
        except error_type as exc:
            propagate(exc)
            reason = getattr(exc, "reason", type(exc).__name__)
            if getattr(exc, "status", None) in (402, 403):
                for symbol in symbols[start:]:
                    fail(symbol, "batch_quote_access_denied:" + reason)
                break
            if getattr(exc, "status", None) in (404, 410):
                missing.extend(batch)
            else:
                for symbol in batch:
                    fail(symbol, "batch_quote_unavailable:" + reason)
            continue
        for symbol in batch:
            row, reason = _select(rows, symbol)
            if row is not None:
                quoted[symbol] = (row, "/stable/batch-quote")
            elif any(str(r.get("symbol") or "") == symbol for r in rows):
                fail(symbol, reason)
            else:
                missing.append(symbol)

    async def one(symbol):
        try:
            rows = await client._request("/stable/quote", {"symbol": symbol})
        except error_type as exc:
            propagate(exc)
            fail(symbol, "quote_unavailable:" + getattr(exc, "reason", type(exc).__name__))
            return
        row, reason = _select(rows, symbol)
        if row is None:
            fail(symbol, reason)
        else:
            quoted[symbol] = (row, "/stable/quote")

    async def bounded(items, fn):
        iterator = iter(items)
        async def worker():
            for item in iterator:
                await fn(item)
        tasks = [asyncio.create_task(worker()) for _ in range(max(1, getattr(client, "concurrency", 1)))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    await bounded(missing, one)

    async def apply(symbol):
        row, source = quoted[symbol]
        original = profiles[symbol]
        stamp = quote_timestamp(row.get("timestamp"))
        try:
            require_quote_current({"as_of": stamp.isoformat() if stamp else ""}, now)
        except DataFreshnessError as exc:
            fail(symbol, str(exc))
            return
        if not _positive(row.get("marketCap")) or not _positive(row.get("price")):
            fail(symbol, "missing_or_invalid_current_quote_cap_or_price")
            return
        if row.get("currency") and str(row["currency"]) != str(original.get("currency")):
            fail(symbol, "quote_currency_conflicts_with_profile")
            return
        refreshed = normalize_profile(dict(original, marketCap=row["marketCap"],
                            mktCap=row["marketCap"], price=row["price"], timestamp=row["timestamp"]))
        if refreshed.get("cap_amount") is None:
            current = row
            if not _positive(current.get("sharesOutstanding")) and getattr(client, "_legacy_available", False):
                try:
                    rows = await client._request("/api/v3/quote/" + quote(symbol, safe=""))
                except error_type as exc:
                    propagate(exc)
                    if getattr(exc, "reason", "") == "legacy_endpoint_retired":
                        client._legacy_available = False
                    fail(symbol, "subunit_quote_enrichment:" + getattr(exc, "reason", type(exc).__name__))
                    return
                current, reason = _select(rows, symbol)
                if current is None:
                    fail(symbol, "subunit_" + reason)
                    return
                # Reconcile an internally coherent quote, not new price/shares
                # combined with an older batch cap. Its date must also be fresh.
                stamp = quote_timestamp(current.get("timestamp"))
                try:
                    require_quote_current({"as_of": stamp.isoformat() if stamp else ""}, now)
                except DataFreshnessError as exc:
                    fail(symbol, str(exc))
                    return
                if not _positive(current.get("marketCap")) or not _positive(current.get("price")):
                    fail(symbol, "subunit_enrichment_missing_current_cap_or_price")
                    return
                if current.get("currency") and str(current["currency"]) != str(original.get("currency")):
                    fail(symbol, "subunit_quote_currency_conflicts_with_profile")
                    return
                refreshed = normalize_profile(dict(original, marketCap=current["marketCap"],
                    mktCap=current["marketCap"], price=current["price"], timestamp=current["timestamp"]))
                row, source = current, "/api/v3/quote"
            refreshed, issue = reconcile_cap_units(refreshed, current)
            if issue is not None or refreshed.get("cap_amount") is None:
                fail(symbol, getattr(issue, "reason", "missing_or_invalid_quote_currency"))
                return
        refreshed.update(as_of=stamp.isoformat(timespec="seconds"), quote_verified=True,
                         quote_source=source, quote_evidence=dict(row),
                         quote_fetched_at=now.isoformat(timespec="seconds"))
        result[symbol] = refreshed

    await bounded(sorted(quoted), apply)
    verified = sum(result[s].get("quote_verified") is True for s in symbols)
    if hasattr(client, "metrics"):
        client.metrics.update(quote_candidates=len(symbols), quotes_verified=verified)
    if hasattr(client, "health"):
        client.health["quote_data_available"] = bool(verified)
        if symbols and not verified:
            client.health["systemic_failure"] = True
    return result, sorted(issues, key=lambda i: (i.symbol, i.stage, i.reason))
