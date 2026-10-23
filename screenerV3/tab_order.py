"""Keep V2 dashboard presentation separate from its raw screening history."""

from calendar import monthrange
from datetime import date
import re

V2_COMPANION_TABS = frozenset({
    'V2 Ranked', 'V2 Run Map', 'V2 Metrics', 'V2 Refresh Log',
})


def history_worksheets(file, worksheets, as_of=None):
    """Keep V2 appearances on cooldown until their 12-month anniversary.

    Unknown or future run dates fail closed instead of silently allowing repeats.
    Preserve the original first-52 physical-tab behavior for other workbooks.
    """
    if getattr(file, 'title', None) != 'V2 Screener':
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
        if worksheet.title in V2_COMPANION_TABS:
            continue
        match = re.fullmatch(r'(\d{1,2})-([A-Z][a-z]{2})-(\d{4})', worksheet.title)
        try:
            if match is None:
                raise ValueError('Run tab does not have a date title')
            day, month, year = match.groups()
            run_date = date(int(year), months[month], int(day))
        except (ValueError, KeyError) as error:
            raise ValueError(f'V2 history has an invalid run date: {worksheet.title!r}') from error
        if run_date > as_of:
            raise ValueError(f'V2 history has a future run date: {worksheet.title!r}')
        eligible_on = date(run_date.year + 1, run_date.month,
                           min(run_date.day, monthrange(run_date.year + 1, run_date.month)[1]))
        if as_of < eligible_on:
            selected.append(worksheet)
    return selected


def output_tab_options(file, kind):
    """Insert after Ranked only when it is already the first V2 tab."""
    if kind == 'beta' and getattr(file, 'title', None) == 'V2 Screener':
        worksheets = file.worksheets()
        if worksheets and worksheets[0].title == 'V2 Ranked':
            return {'index': 1}
    return {}
