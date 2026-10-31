"""Publication-only appearance cooldown for Book/Margin and Negative EV.

Use the approved Revenue Net Cash calendar boundary, never a physical-tab
count. Financial assessments and input evidence are deliberately not filtered.
"""
from datetime import date
import json
import re

from screenerV3.revenue_net_cash_cooldown import appearance_eligible_on


def restore_criteria_order(config, key_order, nested_orders=None):
    """Recover legacy CONFIG order, never numeric/string value canonicalization.

    Coverage serializes sorted JSON but legacy criteria_hash preserves insertion
    order. The callers freeze the producer's original CONFIG/POLICY key order.
    Older versions may omit known keys; unknown schemas fail closed.
    """
    if not isinstance(config, dict) or set(config) - set(key_order):
        raise ValueError('Unknown historical criteria schema')
    nested_orders = nested_orders or {}
    return {key: (restore_criteria_order(config[key], nested_orders[key])
                  if key in nested_orders else config[key])
            for key in key_order if key in config}


def filter_appearances(sheet, batch_date, rows, coverage, prefix, verify, hash_rows):
    seen = set()
    months = {name: i for i, name in enumerate(
        ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
    for tab in sheet.worksheets():
        # Ranked, Run Map, Metrics, Refresh Log, Read me, Configuration,
        # Coverage, Inputs and audit/staging/provisional helpers are not results.
        match = re.fullmatch(r'(\d{1,2})-([A-Z][a-z]{2})-(\d{4})', tab.title)
        if match is None:
            continue
        day, month, year = match.groups()
        try:
            run_date = date(int(year), months[month], int(day))
        except (ValueError, KeyError) as error:
            raise ValueError('Invalid dated result history: ' + tab.title) from error
        if run_date > batch_date:
            raise ValueError('Future dated result history: ' + tab.title)
        # A same-day retry must not suppress its own completed membership.
        if run_date == batch_date or batch_date >= appearance_eligible_on(run_date):
            continue
        note = tab.get_note('A1')
        if not note.startswith(prefix):
            # Unverified manual or unfinished dated writes are not appearances.
            continue
        receipt = json.loads(note[len(prefix):])
        values = sheet.worksheet('Coverage ' + tab.title).get_all_values()
        criteria = [row[1] for row in values if len(row) > 1 and row[0] == 'Criteria']
        if len(criteria) != 1:
            raise ValueError('Missing or ambiguous history criteria: ' + tab.title)
        # Verify result/date identity, coverage/input hashes and companion seals.
        # Read errors or incomplete evidence must abort before any sheet writes.
        if verify(sheet, run_date, json.loads(criteria[0]), historical=True) != receipt:
            raise ValueError('Completed history verification failed: ' + tab.title)
        actual = tab.get_all_values()
        if not actual or not actual[0] or actual[0][0] != 'Ticker':
            raise ValueError('Unexpected completed history header: ' + tab.title)
        # Consume only the exact matrix bound to the receipt: a concurrent edit
        # after verify() must not silently change the appearance set.
        width = len(actual[0])
        if hash_rows([row + [''] * (width - len(row)) for row in actual]) != receipt.get('content_hash'):
            raise ValueError('Completed history changed while reading: ' + tab.title)
        # Exact ticker identity only: do not merge aliases, exchanges or case.
        seen.update(str(row[0]) for row in actual[1:] if row and str(row[0]))
    published = [rows[0]] + [row for row in rows[1:] if str(row[0]) not in seen]
    coverage = dict(coverage)
    coverage.update(qualifying_rows_before_cooldown=len(rows) - 1,
                    cooldown_suppressed_rows=len(rows) - len(published),
                    matched_rows=len(published) - 1,
                    appearance_cooldown='12 calendar months since prior verified published appearance; anniversary eligible')
    return published, coverage
