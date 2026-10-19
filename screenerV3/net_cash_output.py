"""Private, verified Sheet output. Never publish candidate data in Actions artifacts."""
from datetime import datetime, timezone
import hashlib
import json
import os
import time

TITLE = "Net Cash & High Margin Screener"
PREFIX = "net-cash-screen-v1:"
STAGING = "net-cash-staging-v1:"


def stable_hash(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def strings(rows):
    # Store exact decimal strings, avoiding float/locale conversions in audits.
    return [["" if cell is None else str(cell) for cell in row] for row in rows]


def pad(rows, width):
    return [row + [""] * (width - len(row)) for row in rows]


def write_matrix(sheet, title, rows, overwrite=False):
    import gspread
    rows = strings(rows)
    width = max(map(len, rows))
    rows = pad(rows, width)
    try:
        tab = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        tab = sheet.add_worksheet(title=title, rows=max(len(rows), 100), cols=max(width, 10))
        tab.update_note("A1", STAGING + os.getenv("GITHUB_RUN_ID", "local"))
    else:
        if not overwrite:
            existing = pad(tab.get_all_values(), width)
            if existing == rows:
                return tab
            if tab.get_note("A1") != STAGING + os.getenv("GITHUB_RUN_ID", "local"):
                raise ValueError("Existing dated output differs; no automatic overwrite")
            # Only our own incomplete write may be retried. Confirmed outputs remain immutable.
        tab.resize(rows=max(len(rows), 100), cols=max(width, 10))
        tab.clear()
    for offset in range(0, len(rows), 500):
        tab.update(range_name=f"A{offset + 1}", values=rows[offset:offset + 500], value_input_option="RAW")
        if offset + 500 < len(rows):
            time.sleep(1.1)  # Google Sheets per-user write quota, including large audits.
    actual = pad(tab.get_all_values(), width)
    if actual != rows:
        raise ValueError("Private sheet read-back differs from intended output")
    tab.freeze(rows=1)
    return tab


def open_destination(path):
    import gspread
    client = gspread.service_account(filename=path)
    # Refuse ambiguous same-name files. Only the separately created target is touched.
    matches = client.openall(TITLE)
    if len(matches) != 1:
        raise ValueError("Expected exactly one accessible new-screen spreadsheet")
    sheet = matches[0]
    if sheet.title != TITLE:
        raise ValueError("Unexpected destination")
    return sheet


def resume_complete(sheet, batch_date, config):
    import gspread
    title = f"{batch_date.day}-{batch_date.strftime('%b')}-{batch_date.year}"
    try:
        tab = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return None
    note = tab.get_note("A1")
    if not note.startswith(PREFIX):
        return None
    receipt = json.loads(note[len(PREFIX):])
    actual = tab.get_all_values()
    width = len(actual[0]) if actual else 0
    if (receipt.get("batch_date") != batch_date.isoformat()
            or receipt.get("criteria_hash") != stable_hash(config)
            or stable_hash(pad(actual, width)) != receipt.get("content_hash")):
        raise ValueError("Existing completed batch failed verification")
    coverage = sheet.worksheet("Coverage " + title).get_all_values()
    if stable_hash(pad(coverage, 2)) != receipt.get("coverage_hash"):
        raise ValueError("Existing coverage verification failed")
    if receipt.get("inputs_hash"):
        inputs = sheet.worksheet("Inputs " + title).get_all_values()
        if stable_hash(pad(inputs, 4)) != receipt["inputs_hash"]:
            raise ValueError("Existing inputs verification failed")
    return receipt


def publish(sheet, batch_date, rows, audit, coverage, config, records=None):
    title = f"{batch_date.day}-{batch_date.strftime('%b')}-{batch_date.year}"
    config_hash = stable_hash(config)
    # Audit and coverage are explicit, not hidden behind an apparently complete result.
    audit_tab = write_matrix(sheet, "Latest assessment audit", audit, overwrite=True)
    if records:
        write_matrix(sheet, "Inputs " + title, records)
    coverage_rows = [["Field", "Value"]] + [[k, v] for k, v in coverage.items()]
    coverage_rows += [["Batch date", batch_date.isoformat()], ["Criteria", json.dumps(config, sort_keys=True)],
                      ["Latest audit", "Latest assessment audit"],
                      ["Limitations", "FMP coverage only. Unresolved data are not failed criteria; no guarantee every global issuer is covered."]]
    write_matrix(sheet, "Coverage " + title, coverage_rows)
    tab = write_matrix(sheet, title, rows)
    receipt = {"batch_date": batch_date.isoformat(), "status": coverage["status"],
               "tab_id": tab.id, "criteria_hash": config_hash,
               "content_hash": stable_hash(strings(rows)),
               "coverage_hash": stable_hash(strings(coverage_rows)),
               "inputs_hash": stable_hash(strings(records)) if records else None,
               "verified_at": datetime.now(timezone.utc).isoformat(),
               "origin_run_id": os.getenv("GITHUB_RUN_ID"), "commit": os.getenv("GITHUB_SHA")}
    tab.update_note("A1", PREFIX + json.dumps(receipt, sort_keys=True))
    if tab.get_note("A1") != PREFIX + json.dumps(receipt, sort_keys=True):
        raise ValueError("Checkpoint verification failed")
    return receipt
