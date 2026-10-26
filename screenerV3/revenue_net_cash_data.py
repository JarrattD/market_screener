"""Inputs for revenue/net-cash screening; no ownership or extra financial gates.

Shares evidence is provider-internal validation, not an independent issuer audit.
Unproven ADR ratios or share-class scope remain unresolved rather than treating a
listing's capitalization as the whole issuer's capitalization.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from .negative_ev_data import FMPClient as BalanceClient
from .negative_ev_provider import FMPError, FMPAuthenticationError, Issue, _map_bounded, utc_now, currency_units
from .book_margin_data import AnnualIncome, available_statements, latest_statement
from .data_freshness import quote_timestamp, require_quote_current, DataFreshnessError


def _positive(value):
    if isinstance(value, bool) or value in (None, ''):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError):
        return None


def issuer_cap_evidence(profile, rows, now=None):
    """Bind independent outstanding-share evidence to a current ordinary quote.

    There is deliberately no float-share fallback. Explicit ADR or class
    ambiguity cannot be repaired by a numerically matching listing cap.
    """
    now = now or datetime.now(timezone.utc)
    symbol = profile.get('symbol')
    result = dict(profile, whole_issuer_cap_verified=False, whole_issuer_cap_evidence={})
    def fail(reason):
        result['whole_issuer_cap_reason'] = reason
        return result, reason
    if profile.get('security_eligible') is not True or profile.get('quote_verified') is not True:
        return fail('current_active_equity_quote_unverified')
    try:
        require_quote_current(profile, now)
    except DataFreshnessError:
        return fail('current_quote_date_unverified')
    if profile.get('isAdr') is not False:
        return fail('adr_ratio_or_non_adr_status_unverified')
    name = str(profile.get('companyName') or '')
    if (profile.get('multipleShareClasses') is True or profile.get('shareClass') not in (None, '', 'Ordinary', 'Common')
            or re.search(r'\bclass\s+[a-z0-9]+\b', name, re.I)):
        return fail('whole_issuer_share_class_scope_unverified')
    candidates = [r for r in rows if r.get('symbol') == symbol]
    dated = [(quote_timestamp(r.get('date') or r.get('timestamp')), r) for r in candidates]
    # Future records cannot establish a currently outstanding share count.
    dated = [(stamp, row) for stamp, row in dated if stamp is not None and stamp <= now]
    if not dated:
        return fail('shares_date_missing_or_future')
    latest = max(stamp for stamp, _ in dated)
    if latest < now - timedelta(days=7):
        return fail('shares_evidence_stale')
    latest_rows = [r for stamp, r in dated if stamp == latest]
    signatures = {(str(r.get('outstandingShares')), str(r.get('cik') or '').lstrip('0'), str(r.get('isin') or '')) for r in latest_rows}
    if len(signatures) != 1:
        return fail('conflicting_latest_outstanding_shares')
    shares_row = latest_rows[0]
    for key in ('cik', 'isin'):
        left, right = str(profile.get(key) or '').strip(), str(shares_row.get(key) or '').strip()
        if key == 'cik': left, right = left.lstrip('0'), right.lstrip('0')
        if left and right and left != right:
            return fail('shares_issuer_identity_conflict')
    shares, price, cap = (_positive(shares_row.get('outstandingShares')),
                          _positive(profile.get('price')), _positive(profile.get('cap_amount')))
    currency, multiplier = currency_units(str(profile.get('currency') or ''))
    if not shares or not price or not cap or currency != profile.get('cap_currency'):
        return fail('current_price_shares_or_cap_units_missing')
    product = price * shares * Decimal(str(multiplier))
    difference = abs(product / cap - 1)
    if difference > Decimal('0.01'):
        return fail('whole_issuer_price_times_shares_mismatch')
    result.update(whole_issuer_cap_verified=True, whole_issuer_cap_reason='',
        whole_issuer_cap_evidence={'scope': 'whole_issuer', 'validated': True,
            'cap_amount': profile['cap_amount'], 'cap_currency': profile['cap_currency'],
            'source': '/stable/shares-float', 'shares': str(shares), 'shares_date': latest.isoformat(),
            'quote_as_of': profile['as_of'], 'quote_multiplier': str(multiplier),
            'price_times_shares': str(product), 'relative_difference': str(difference),
            'tolerance': '0.01', 'security_basis': 'active non-ADR ordinary equity; no indicated multiple classes',
            'limitations': 'FMP provider-internal validation; not independent issuer audit',
            'raw_shares': shares_row})
    return result, None


class FMPClient(BalanceClient):
    async def fetch_annual_income(self, symbols):
        result, issues = {}, []
        symbols = sorted(set(symbols))
        async def one(symbol):
            rows, error = await self.statements(symbol, 'income-statement', 'annual', 8)
            rows = available_statements(rows, symbol)
            latest = None if error else latest_statement(rows, symbol, annual_only=True)
            result[symbol] = AnnualIncome(latest, rows, utc_now(), error)
            if latest is None:
                issues.append(Issue(symbol, 'annual_income', error or 'missing_or_conflicting_latest_annual_income'))
        await _map_bounded(symbols, one, self.concurrency)
        self.metrics['annual_income_candidates'] = len(symbols)
        self.health['annual_income_available'] = any(x.latest is not None for x in result.values())
        if symbols and not self.health['annual_income_available']:
            self.health['systemic_failure'] = True
        return result, sorted(issues, key=lambda x: (x.symbol, x.reason))

    async def verify_issuer_caps(self, profiles):
        result = {s: dict(p, whole_issuer_cap_verified=False, whole_issuer_cap_evidence={}) for s, p in profiles.items()}
        issues, request_failures = [], []
        async def one(symbol):
            try:
                rows = await self._request('/stable/shares-float', {'symbol': symbol})
            except FMPAuthenticationError:
                raise
            except FMPError as exc:
                if self._fatal is not None:
                    raise self._fatal
                request_failures.append(symbol)
                result[symbol]['whole_issuer_cap_reason'] = exc.reason
                issues.append(Issue(symbol, 'whole_issuer_cap', exc.reason))
                return
            result[symbol], reason = issuer_cap_evidence(result[symbol], rows)
            if reason:
                issues.append(Issue(symbol, 'whole_issuer_cap', reason))
        symbols = sorted(s for s, p in profiles.items() if p.get('security_eligible') is True and p.get('quote_verified') is True)
        await _map_bounded(symbols, one, self.concurrency)
        if symbols and len(request_failures) == len(symbols):
            self.health['systemic_failure'] = True
        self.metrics['issuer_cap_candidates'] = len(symbols)
        self.metrics['issuer_caps_verified'] = sum(p.get('whole_issuer_cap_verified') is True for p in result.values())
        return result, sorted(issues, key=lambda x: (x.symbol, x.reason))
