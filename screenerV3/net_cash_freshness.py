"""Adapt the shared data-quality contract to this screen's missing-data type."""
from . import data_freshness as shared
from .net_cash_calculations import MissingData
POLICY = shared.POLICY
cutoff_months = shared.cutoff_months

def require_statement_current(row, label, today=None):
    try:
        return shared.require_statement_current(row, label, today)
    except shared.DataFreshnessError as exc:
        raise MissingData(str(exc)) from None

def require_quote_current(profile, now=None):
    try:
        return shared.require_quote_current(profile, now)
    except shared.DataFreshnessError as exc:
        raise MissingData(str(exc)) from None
