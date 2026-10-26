"""Publication-only cooldown for the two independent, receipt-verified screens.

Financial assessments and their audit/input records are never filtered. Only
prior completed dated result rows constitute an appearance, within this workbook.
"""
from datetime import date
import json
import re

from calendar import monthrange

def appearance_eligible_on(day):
    return date(day.year + 1, day.month, min(day.day, monthrange(day.year + 1, day.month)[1]))


def filter_appearances(sheet, batch_date, rows, coverage, prefix, verify):
    seen = set()
    months = {name: i for i, name in enumerate(
        ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
    for tab in sheet.worksheets():
        # Helpers, audit, coverage and inputs are not raw result appearances.
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
        # Same-day reruns must not suppress their own rows; immutable completed
        # batches are separately checked by the publisher before any writes.
        if run_date == batch_date or batch_date >= appearance_eligible_on(run_date):
            continue
        note = tab.get_note('A1')
        if not note.startswith(prefix):
            # Manual tabs and unfinished staging writes are not completed runs.
            continue
        receipt = json.loads(note[len(prefix):])
        if receipt.get('status') not in ('complete', 'complete_with_unresolved') or receipt.get('tab_id') != tab.id:
            raise ValueError('Invalid completed history receipt: ' + tab.title)
        values = sheet.worksheet('Coverage ' + tab.title).get_all_values()
        criteria = [row[1] for row in values if len(row) > 1 and row[0] == 'Criteria']
        if len(criteria) != 1:
            raise ValueError('Missing or ambiguous history criteria: ' + tab.title)
        # Verify result hash, exact date, coverage hash and companion seals/inputs.
        # Historical criteria are hash-bound within Coverage; their original
        # config insertion order (used by criteria_hash) is not recoverable.
        if verify(sheet, run_date, json.loads(criteria[0]), historical=True) != receipt:
            raise ValueError('Completed history verification failed: ' + tab.title)
        actual = tab.get_all_values()
        if not actual or actual[0][0] != 'Ticker':
            raise ValueError('Unexpected completed history header: ' + tab.title)
        seen.update(str(row[0]).strip().upper() for row in actual[1:] if row and str(row[0]).strip())
    published = [rows[0]] + [row for row in rows[1:] if str(row[0]).strip().upper() not in seen]
    suppressed = len(rows) - len(published)
    coverage = dict(coverage)
    coverage.update(qualifying_rows_before_cooldown=len(rows) - 1,
                    cooldown_suppressed_rows=suppressed, matched_rows=len(published) - 1,
                    appearance_cooldown='12 calendar months since prior verified published appearance; anniversary eligible')
    return published, coverage
