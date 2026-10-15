"""Fail-closed, read-back verified handoff. No research or credential creation."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HEADERS = {
    'alpha': ['Ticker', 'Company Name', 'FV Upside', '5Y Price Metric', ' ', 'NCAV Ratio', 'EV/aFCF', 'Payback Rating', 'Average Yield', 'HQ Country'],
    'beta': ['Ticker', 'Company Name', 'NCAV Ratio', 'EV/aFCF', 'P/TBV Ratio', 'HQ Location', ' ', 'FV Upside', '5Y Price Metric'],
}

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def matrix(kind, results):
    rows = [HEADERS[kind]]
    for ticker, v in results.items():
        if kind == 'alpha':
            row = [ticker, str(v['Name']), f"{v['FV Upside Metric']}%", f"{v['5Y Price Metric']}%", ' ', v['NCAV Ratio'], v['EV/aFCF'], v['Payback Rating'], v['5Y average'], str(v['HQ Location'])]
        else:
            row = [ticker, str(v['Name']), v['NCAV Ratio'], v['EV/aFCF'], v['P/TBV Ratio'], str(v['HQ Location']), ' ', f"{v['FV Upside Metric']}%", f"{v['5Y Price Metric']}%"]
        rows.append(row)
    # Fail before writing on NaN/Infinity or unserializable values.
    digest(rows)
    return rows

def canonical(rows, width):
    # gspread omits trailing empty cells; numeric JSON int/float differ only in representation.
    return [[str(float(v)) if isinstance(v, (float, int)) and not isinstance(v, bool) else str(v) for v in list(row) + [''] * (width-len(row))] for row in rows]

def write_verified(sheet, kind, results, batch_date):
    if not results:
        raise ValueError('Empty screener result requires investigation; no ready manifest emitted')
    expected = matrix(kind, results)
    title = f"{batch_date.day}-{batch_date.strftime('%b')}-{batch_date.year}"
    matches = [w for w in sheet.file.worksheets() if w.title == title]
    if len(matches) > 1:
        raise ValueError('Ambiguous destination tab')
    width = len(expected[0])
    if matches:
        worksheet = matches[0]
        actual = worksheet.get_all_values(value_render_option='UNFORMATTED_VALUE')
        if canonical(actual, width) != canonical(expected, width):
            raise ValueError(f'{kind}: existing dated tab differs; refusing overwrite or append. Reconcile partial/corrected run explicitly.')
    else:
        worksheet = sheet.file.add_worksheet(title=title, rows=max(100, len(expected)), cols=width)
        worksheet.update(range_name=f'A1:{chr(64+width)}{len(expected)}', values=expected, value_input_option='RAW')
        actual = worksheet.get_all_values(value_render_option='UNFORMATTED_VALUE')
        if canonical(actual, width) != canonical(expected, width):
            raise ValueError(f'{kind}: read-back differs from intended output')
    return {'kind': kind, 'spreadsheet_id': sheet.file.id, 'tab_id': worksheet.id, 'tab_title': title,
            'row_count': len(expected)-1, 'content_sha256': digest(canonical(actual, width)),
            'verified_at': datetime.now(timezone.utc).isoformat()}

def publish_manifest(receipts, batch_date, path='research_completion.json'):
    if len(receipts) != 2 or {x['kind'] for x in receipts} != {'alpha', 'beta'}:
        raise ValueError('Both outputs required')
    if len({x['spreadsheet_id'] for x in receipts}) != 2 or len({x['tab_title'] for x in receipts}) != 1:
        raise ValueError('Outputs must be distinct spreadsheets for same logical date')
    payload = {'schema_version': 1, 'status': 'both_writes_verified', 'batch_id': batch_date.isoformat(),
               'repository': os.getenv('GITHUB_REPOSITORY'), 'commit': os.getenv('GITHUB_SHA'),
               'run_id': os.getenv('GITHUB_RUN_ID'), 'run_attempt': os.getenv('GITHUB_RUN_ATTEMPT'),
               'outputs': sorted(receipts, key=lambda x: x['kind'])}
    payload['revision_id'] = digest([batch_date.isoformat(), [(x['kind'], x['spreadsheet_id'], x['tab_id'], x['content_sha256']) for x in payload['outputs']]])
    target = Path(path)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, indent=2) + '\n')
    temporary.replace(target)
    return payload

def london_batch_date():
    return datetime.now(ZoneInfo('Europe/London')).date()
