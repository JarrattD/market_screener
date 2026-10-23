"""Keep V2 dashboard presentation separate from its raw screening history."""

V2_COMPANION_TABS = frozenset({
    'V2 Ranked', 'V2 Run Map', 'V2 Metrics', 'V2 Refresh Log',
})


def history_worksheets(file, worksheets):
    """Preserve the existing history order, excluding only known V2 dashboards."""
    if getattr(file, 'title', None) != 'V2 Screener':
        return worksheets
    return [w for w in worksheets if w.title not in V2_COMPANION_TABS]


def output_tab_options(file, kind):
    """Insert after Ranked only when it is already the first V2 tab."""
    if kind == 'beta' and getattr(file, 'title', None) == 'V2 Screener':
        worksheets = file.worksheets()
        if worksheets and worksheets[0].title == 'V2 Ranked':
            return {'index': 1}
    return {}
