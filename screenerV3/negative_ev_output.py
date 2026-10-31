"""Private, verified Sheet output. Never publish candidate data in Actions artifacts."""
from datetime import datetime, timezone
import hashlib
import json
import os
import time
import uuid

from screenerV3.appearance_cooldown import filter_appearances, restore_criteria_order

TITLE = "Negative EV & Positive 5Y FCF Screener"
PREFIX = "negative-ev-fcf-v1:"
STAGING = "negative-ev-staging-v1:"
SEALED = "negative-ev-sealed-v1:"
# Different CLI processes must never share the literal owner name "local".
RUN_OWNER = os.getenv("GITHUB_RUN_ID") or "local-" + uuid.uuid4().hex

# Frozen legacy CONFIG order: Coverage JSON is sorted, criteria_hash is not.
CRITERIA_KEY_ORDER = (
    'version',
    'data_freshness',
    'enterprise_value',
    'ev_strictly_below',
    'cumulative_reported_fcf_strictly_above',
    'fcf_window',
    'include_negative_periods',
    'missing_data',
    'market_cap_ceiling',
    'gross_margin_filter',
    'fcf_ratio_filter',
)
CRITERIA_NESTED_ORDER = {"data_freshness": ("quote_max_age_days",
    "statement_max_age_calendar_months", "missing_or_stale", "version")}


def stable_hash(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def strings(rows):
    # Store exact decimal strings, avoiding float/locale conversions in audits.
    return [["" if cell is None else str(cell) for cell in row] for row in rows]


def pad(rows, width):
    return [row + [""] * (width - len(row)) for row in rows]


def owned(tab):
    note = tab.get_note("A1")
    if note == STAGING + RUN_OWNER:
        return True
    if note.startswith(SEALED):
        try:
            return json.loads(note[len(SEALED):]).get("owner") == RUN_OWNER
        except (ValueError, TypeError):
            return False
    return False


def seal(tab):
    rows = tab.get_all_values()
    width = max(map(len, rows)) if rows else 0
    value = {"owner": RUN_OWNER, "content_hash": stable_hash(pad(rows, width))}
    note = SEALED + json.dumps(value, sort_keys=True)
    tab.update_note("A1", note)
    if tab.get_note("A1") != note:
        raise ValueError("Companion seal verification failed")


def verified_seal(tab, rows, width, expected_owner=None):
    note = tab.get_note("A1")
    if not note.startswith(SEALED):
        raise ValueError("Completed companion is not sealed")
    value = json.loads(note[len(SEALED):])
    if expected_owner is not None and value.get("owner") != str(expected_owner):
        raise ValueError("Completed companion owner differs from receipt origin")
    if value.get("content_hash") != stable_hash(pad(rows, width)):
        raise ValueError("Companion seal content differs")


def write_matrix(sheet, title, rows, overwrite=False, tab_options=None):
    import gspread
    rows = strings(rows)
    width = max(map(len, rows))
    rows = pad(rows, width)
    try:
        tab = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        tab = sheet.add_worksheet(title=title, rows=max(len(rows), 100), cols=max(width, 10), **(tab_options or {}))
        tab.update_note("A1", STAGING + RUN_OWNER)
    else:
        if not overwrite:
            if not owned(tab):
                raise ValueError("Existing dated output is not owned by this run")
            existing = pad(tab.get_all_values(), width)
            if existing == rows:
                return tab
            if tab.get_note("A1") != STAGING + RUN_OWNER:
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


def open_destination(path, spreadsheet_id):
    import gspread
    if not spreadsheet_id or not isinstance(spreadsheet_id, str):
        raise ValueError("Explicit new-screen spreadsheet ID is required")
    client = gspread.service_account(filename=path)
    sheet = client.open_by_key(spreadsheet_id)
    if sheet.title != TITLE or sheet.id != spreadsheet_id:
        raise ValueError("Unexpected destination; no writes performed")
    return sheet


def resume_complete(sheet, batch_date, config, *, historical=False):
    """Verify immutable output, including legacy historical criteria hashes.

    Historical Coverage JSON sorts keys; recover the frozen original producer
    order before hashing. Current-run config equality is unchanged.
    """
    if historical:
        config = restore_criteria_order(config, CRITERIA_KEY_ORDER, CRITERIA_NESTED_ORDER)
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
    if historical and any(not isinstance(receipt.get(key), str) or not receipt[key].strip()
                          for key in ("origin_run_id", "commit")):
        raise ValueError("Historical appearance lacks production origin")
    actual = tab.get_all_values()
    width = len(actual[0]) if actual else 0
    if (receipt.get("status") not in ("complete", "complete_with_unresolved")
            or receipt.get("tab_id") != tab.id
            or receipt.get("batch_date") != batch_date.isoformat()
            or receipt.get("criteria_hash") != stable_hash(config)
            or stable_hash(pad(actual, width)) != receipt.get("content_hash")):
        raise ValueError("Existing completed batch failed verification")
    coverage_tab = sheet.worksheet("Coverage " + title)
    coverage = coverage_tab.get_all_values()
    if stable_hash(pad(coverage, 2)) != receipt.get("coverage_hash"):
        raise ValueError("Existing coverage verification failed")
    verified_seal(coverage_tab, coverage, 2, receipt.get("origin_run_id"))
    if receipt.get("inputs_hash"):
        inputs_tab = sheet.worksheet("Inputs " + title)
        inputs = inputs_tab.get_all_values()
        if stable_hash(pad(inputs, 4)) != receipt["inputs_hash"]:
            raise ValueError("Existing inputs verification failed")
        verified_seal(inputs_tab, inputs, 4, receipt.get("origin_run_id"))
    return receipt


def publish(sheet, batch_date, rows, audit, coverage, config, records=None):
    import gspread
    title = f"{batch_date.day}-{batch_date.strftime('%b')}-{batch_date.year}"
    config_hash = stable_hash(config)
    # A completed receipt is checked BEFORE mutating audit or companion tabs.
    # This also protects a second invocation that started before the first
    # invocation finished and therefore did not see its initial checkpoint.
    existing = resume_complete(sheet, batch_date, config)
    if existing and existing.get("content_hash") == stable_hash(strings(rows)):
        # Also preserve pre-cooldown completed batches on same-day resume.
        return existing
    rows, coverage = filter_appearances(sheet, batch_date, rows, coverage,
                                        PREFIX, resume_complete, stable_hash)
    if existing:
        if existing.get("content_hash") != stable_hash(strings(rows)):
            raise ValueError("Conflicting completed batch; no writes performed")
        return existing
    tabs = sheet.worksheets()
    if not tabs or tabs[0].title != "Negative EV Ranked":
        raise ValueError("Expected Ranked as the first tab; no raw output written")
    dated_titles = [title, "Coverage " + title] + (["Inputs " + title] if records else [])
    for dated_title in dated_titles:
        try:
            candidate = sheet.worksheet(dated_title)
        except gspread.WorksheetNotFound:
            continue
        if not owned(candidate):
            raise ValueError("Dated output belongs to another run; no writes performed")
    # Audit and coverage are explicit, not hidden behind an apparently complete result.
    audit_tab = write_matrix(sheet, "Latest assessment audit", audit, overwrite=True)
    inputs_tab = None
    if records:
        inputs_tab = write_matrix(sheet, "Inputs " + title, records)
    coverage_rows = [["Field", "Value"]] + [[k, v] for k, v in coverage.items()]
    coverage_rows += [["Batch date", batch_date.isoformat()], ["Criteria", json.dumps(config, sort_keys=True)],
                      ["Latest audit", "Latest assessment audit"],
                      ["Limitations", "FMP coverage only. Unresolved data are not failed criteria; no guarantee every global issuer is covered."]]
    coverage_tab = write_matrix(sheet, "Coverage " + title, coverage_rows)
    tab = write_matrix(sheet, title, rows, tab_options={"index": 1})
    # Companion tables become immutable before the final completion marker.
    seal(coverage_tab)
    if inputs_tab is not None:
        seal(inputs_tab)
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


