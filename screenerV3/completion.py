"""Fail-closed, read-back verified handoff. No research or credential creation."""
import hashlib
import json
import os
import re
from urllib.request import urlopen
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

RECEIPT_PREFIX = 'screener-completion-v1:'

def tab_title(batch_date):
    return f"{batch_date.day}-{batch_date.strftime('%b')}-{batch_date.year}"

def resume_verified(sheet, kind, batch_date):
    """Read private checkpoint before constructing screener (which filters seen tickers)."""
    matches = [w for w in sheet.file.worksheets() if w.title == tab_title(batch_date)]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('Ambiguous destination tab')
    worksheet = matches[0]
    note = worksheet.get_note('A1')
    if not note.startswith(RECEIPT_PREFIX):
        raise ValueError(f'{kind}: existing tab has no verified checkpoint; explicit reconciliation required')
    receipt = json.loads(note[len(RECEIPT_PREFIX):])
    if (receipt.get('kind') != kind or receipt.get('batch_id') != batch_date.isoformat()
            or receipt.get('spreadsheet_id') != sheet.file.id or receipt.get('tab_id') != worksheet.id
            or receipt.get('tab_title') != tab_title(batch_date)):
        raise ValueError('Private checkpoint identity mismatch')
    run_id = os.getenv('GITHUB_RUN_ID')
    if run_id and receipt.get('origin_run_id') != run_id:
        raise ValueError('Private checkpoint belongs to another workflow run')
    actual = worksheet.get_all_values(value_render_option='UNFORMATTED_VALUE')
    # Explicit render option required for numeric hash stability.
    width = len(HEADERS[kind])
    if (not actual or actual[0] != HEADERS[kind] or len(actual) < 2
            or len(actual)-1 != receipt.get('row_count')
            or digest(canonical(actual, width)) != receipt.get('content_sha256')):
        raise ValueError('Private checkpoint read-back mismatch')
    return receipt

def write_verified(sheet, kind, results, batch_date):
    if not results:
        raise ValueError('Empty screener result requires investigation; no ready manifest emitted')
    expected = matrix(kind, results)
    existing = resume_verified(sheet, kind, batch_date)
    width = len(expected[0])
    if existing is not None:
        if existing['content_sha256'] != digest(canonical(expected, width)):
            raise ValueError('Existing verified output differs; refusing overwrite')
        return existing
    title = tab_title(batch_date)
    worksheet = sheet.file.add_worksheet(title=title, rows=max(100, len(expected)), cols=width)
    worksheet.update(range_name=f'A1:{chr(64+width)}{len(expected)}', values=expected, value_input_option='RAW')
    actual = worksheet.get_all_values(value_render_option='UNFORMATTED_VALUE')
    if canonical(actual, width) != canonical(expected, width):
        raise ValueError(f'{kind}: read-back differs from intended output')
    receipt = {'kind': kind, 'batch_id': batch_date.isoformat(), 'spreadsheet_id': sheet.file.id,
               'tab_id': worksheet.id, 'tab_title': title, 'row_count': len(expected)-1,
               'content_sha256': digest(canonical(actual, width)),
               'verified_at': datetime.now(timezone.utc).isoformat(),
               'origin_run_id': os.getenv('GITHUB_RUN_ID'), 'origin_run_attempt': os.getenv('GITHUB_RUN_ATTEMPT')}
    # Only a newly created tab is annotated. Existing notes/formatting are never overwritten.
    worksheet.update_note('A1', RECEIPT_PREFIX + json.dumps(receipt, separators=(',', ':')))
    return resume_verified(sheet, kind, batch_date)

def publish_manifest(receipts, batch_date, path='research_completion.json'):
    if len(receipts) != 2 or {x['kind'] for x in receipts} != {'alpha', 'beta'}:
        raise ValueError('Both outputs required')
    if len({x['spreadsheet_id'] for x in receipts}) != 2 or any(x['tab_title'] != tab_title(batch_date) or x.get('batch_id') != batch_date.isoformat() for x in receipts):
        raise ValueError('Outputs must be distinct spreadsheets for same logical date')
    run_id = os.getenv('GITHUB_RUN_ID')
    if run_id and any(x.get('origin_run_id') != run_id for x in receipts):
        raise ValueError('Both private outputs must originate in the current workflow run')
    payload = {'schema_version': 2, 'status': 'both_writes_verified', 'batch_id': batch_date.isoformat(),
               'repository': os.getenv('GITHUB_REPOSITORY'), 'commit': os.getenv('GITHUB_SHA'),
               'run_id': os.getenv('GITHUB_RUN_ID'), 'run_attempt': os.getenv('GITHUB_RUN_ATTEMPT'),
               'outputs': ['alpha', 'beta']}
    # No spreadsheet/tab IDs, counts, content hashes or private receipts in public artifacts.
    target = Path(path)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, indent=2) + '\n')
    temporary.replace(target)
    return payload

def london_batch_date():
    run_id = os.getenv('GITHUB_RUN_ID')
    repository = os.getenv('GITHUB_REPOSITORY')
    if run_id or repository:
        if not run_id or not run_id.isdigit() or not repository or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid workflow run identity')
        # Public repository GET only; no token or added credential scopes. The original
        # creation date survives reruns after midnight; API failure deliberately blocks.
        with urlopen(f'https://api.github.com/repos/{repository}/actions/runs/{run_id}', timeout=30) as response:
            run = json.load(response)
        if str(run.get('id')) != run_id:
            raise ValueError('Workflow run identity mismatch')
        created = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
        return created.astimezone(ZoneInfo('Europe/London')).date()
    return datetime.now(ZoneInfo('Europe/London')).date()
