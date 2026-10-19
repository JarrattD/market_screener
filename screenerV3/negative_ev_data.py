"""Balance-first inputs for the independent negative-EV screen.

No annual income, market-cap ceiling, gross-margin filter, or ratio gate is
requested. Every eligible stock reaches the balance stage. Only verified
negative-EV stocks need the longer cash-flow history.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote
import asyncio

from .negative_ev_provider import (
    FMPClient as ProviderClient, FMPError, FMPAuthenticationError, Issue,
    latest_statement, utc_now, _map_bounded,
)


@dataclass
class CashflowHistory:
    annual: list = field(default_factory=list)
    interim: list = field(default_factory=list)
    ttm: list = field(default_factory=list)
    fetched_at: str = ""
    errors: dict = field(default_factory=dict)


def available_statements(rows):
    """Exclude a future filing as well as a future financial period."""
    today = datetime.now(timezone.utc).date().isoformat()
    return [row for row in rows if all(
        not row.get(key) or str(row[key])[:10] <= today
        for key in ("date", "filingDate", "fillingDate", "acceptedDate"))]


class FMPClient(ProviderClient):
    async def statements(self, symbol, endpoint, period, limit):
        try:
            rows = await self._compatible("/stable/" + endpoint,
                "/api/v3/" + endpoint + "/" + quote(symbol, safe=""),
                {"symbol": symbol, "period": period, "limit": limit})
            return available_statements(rows), None
        except FMPAuthenticationError:
            raise
        except FMPError as exc:
            if self._fatal is not None:
                raise self._fatal
            return [], exc.reason

    async def fetch_balances(self, symbols):
        result, issues = {}, []
        symbols = sorted(set(symbols))

        async def one(symbol):
            answers = await asyncio.gather(
                self.statements(symbol, "balance-sheet-statement", "annual", 3),
                self.statements(symbol, "balance-sheet-statement", "quarter", 4))
            errors = [error for _, error in answers if error]
            # A failed frequency cannot establish which frequency is newest.
            if errors:
                result[symbol] = None
                issues.append(Issue(symbol, "balance", ";".join(sorted(set(errors)))))
                return
            result[symbol] = latest_statement(answers[0][0] + answers[1][0], symbol)
            if result[symbol] is None:
                issues.append(Issue(symbol, "balance", "missing_valid_latest_balance"))

        await _map_bounded(symbols, one, self.concurrency)
        self.metrics["balance_candidates"] = len(symbols)
        self.health["balance_data_available"] = any(row is not None for row in result.values())
        if symbols and not self.health["balance_data_available"]:
            self.health["systemic_failure"] = True
        return result, sorted(issues, key=lambda i: (i.symbol, i.reason))

    async def fetch_cashflow_history(self, symbols):
        result, issues = {}, []

        async def one(symbol):
            # These are record limits, not universe truncation. Eight FYs and
            # 32 interims leave room for restatements and the opening boundary.
            annual, interim = await asyncio.gather(
                self.statements(symbol, "cash-flow-statement", "annual", 8),
                self.statements(symbol, "cash-flow-statement", "quarter", 32))
            errors = {name: error for name, (_, error) in
                      (("annual", annual), ("interim", interim)) if error}
            ttm = []
            try:
                ttm = available_statements(await self._request(
                    "/stable/cash-flow-statement-ttm", {"symbol": symbol, "limit": 8}))
            except FMPAuthenticationError:
                raise
            except FMPError as exc:
                if self._fatal is not None:
                    raise self._fatal
                # Optional TTM access failure can still permit a clearly
                # labelled five-FY fallback. Never bypass a denied endpoint.
                errors["ttm"] = exc.reason
            for stage, reason in errors.items():
                issues.append(Issue(symbol, "cashflow_" + stage, reason))
            result[symbol] = CashflowHistory(annual[0], interim[0], ttm, utc_now(), errors)

        symbols = sorted(set(symbols))
        await _map_bounded(symbols, one, self.concurrency)
        self.metrics["cashflow_candidates"] = len(symbols)
        self.health["cashflow_data_available"] = any(
            h.annual or h.interim or h.ttm for h in result.values())
        if symbols and not self.health["cashflow_data_available"]:
            self.health["systemic_failure"] = True
        return result, sorted(issues, key=lambda i: (i.symbol, i.stage, i.reason))
