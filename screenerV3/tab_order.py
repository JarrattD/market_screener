"""Keep dashboard presentation separate from its raw screening history."""

from calendar import monthrange
from datetime import date
import re

V2_COMPANION_TABS = frozenset({
    'V2 Ranked', 'V2 Run Map', 'V2 Metrics', 'V2 Refresh Log',
})


MAIN_COMPANION_TABS = frozenset({
    'Main Ranked', 'Main Run Map', 'Main Metrics', 'Main Refresh Log',
})
COMPANION_TABS = {'V2 Screener': V2_COMPANION_TABS, 'Screener': MAIN_COMPANION_TABS}


def appearance_eligible_on(run_date):
    """The exact 12-calendar-month anniversary, clamped for leap day."""
    return date(run_date.year + 1, run_date.month,
                min(run_date.day, monthrange(run_date.year + 1, run_date.month)[1]))


def history_worksheets(file, worksheets, as_of=None):
    """Keep main and V2 appearances on cooldown until their 12-month anniversary.

    Unknown or future run dates fail closed instead of silently allowing repeats.
    Preserve the original first-52 physical-tab behavior for other workbooks.
    """
    workbook_title = getattr(file, 'title', None)
    companions = COMPANION_TABS.get(workbook_title)
    if companions is None:
        return worksheets[:52]
    if as_of is None:
        # Same verified London logical run date used by the production writer;
        # reruns retain the original workflow creation date, not today's date.
        from screenerV3.completion import london_batch_date
        as_of = london_batch_date()
    months = {name: i for i, name in enumerate(
        ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
    selected = []
    for worksheet in worksheets:
        if worksheet.title in companions:
            continue
        match = re.fullmatch(r'(\d{1,2})-([A-Z][a-z]{2})-(\d{4})', worksheet.title)
        try:
            if match is None:
                raise ValueError('Run tab does not have a date title')
            day, month, year = match.groups()
            run_date = date(int(year), months[month], int(day))
        except (ValueError, KeyError) as error:
            raise ValueError(f'{workbook_title} history has an invalid run date: {worksheet.title!r}') from error
        if run_date > as_of:
            raise ValueError(f'{workbook_title} history has a future run date: {worksheet.title!r}')
        eligible_on = appearance_eligible_on(run_date)
        if as_of < eligible_on:
            selected.append(worksheet)
    return selected


def output_tab_options(file, kind):
    """Insert after Ranked only when it is already the first tab in its workbook."""
    ranked_title = {('beta', 'V2 Screener'): 'V2 Ranked',
                    ('alpha', 'Screener'): 'Main Ranked',
                    ('book-margin', 'Low Book Value & High Gross Margin Screener'): 'Book Margin Ranked',
                    ('negative-ev-fcf', 'Negative EV & Positive 5Y FCF Screener'): 'Negative EV Ranked'}.get(
                        (kind, getattr(file, 'title', None)))
    if ranked_title is not None:
        worksheets = file.worksheets()
        if worksheets and worksheets[0].title == ranked_title:
            return {'index': 1}
    return {}
