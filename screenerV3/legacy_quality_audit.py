"""Private dated data-quality evidence for the two existing screeners."""
import hashlib
import json
import os
import re
import time

HEADERS = ["Ticker", "Data status", "Reason", "Source", "Source date", "Checked UTC"]
PREFIX = "legacy-quality-v1:"


def result_tab(title):
    return re.fullmatch(r"\d{1,2}-[A-Za-z]{3}-\d{4}", title) is not None


def write_quality_audit(sheet, kind, rows, batch_date):
    title = f"Data quality {batch_date.day}-{batch_date.strftime('%b')}-{batch_date.year}"
    expected = [HEADERS] + [[str(value or "") for value in row] for row in rows]
    digest = hashlib.sha256(json.dumps(expected, separators=(",", ":")).encode()).hexdigest()
    matches = [tab for tab in sheet.file.worksheets() if tab.title == title]
    if matches:
        if len(matches) != 1:
            raise ValueError("Ambiguous private quality audit")
        tab = matches[0]
        if tab.get_all_values() != expected:
            raise ValueError("Existing quality audit differs; no overwrite")
        note = tab.get_note("A1")
        if not note.startswith(PREFIX):
            raise ValueError("Existing quality audit is unverified")
        receipt = json.loads(note[len(PREFIX):])
        if (receipt.get("kind") != kind or receipt.get("batch") != str(batch_date)
                or receipt.get("hash") != digest
                or (os.getenv("GITHUB_RUN_ID") and receipt.get("origin_run_id") != os.getenv("GITHUB_RUN_ID"))):
            raise ValueError("Existing quality audit checkpoint differs")
        return digest
    tab = sheet.file.add_worksheet(title=title, rows=max(len(expected),100), cols=6)
    for offset in range(0,len(expected),500):
        tab.update(range_name=f"A{offset+1}",values=expected[offset:offset+500],value_input_option="RAW")
        if offset+500<len(expected):time.sleep(1.1)
    actual = [row+[""]*(6-len(row)) for row in tab.get_all_values()]
    if actual != expected:
        raise ValueError("Quality audit read-back differs")
    note = PREFIX + json.dumps({"kind":kind,"batch":str(batch_date),"hash":digest,
                              "origin_run_id":os.getenv("GITHUB_RUN_ID")},sort_keys=True)
    tab.update_note("A1",note)
    if tab.get_note("A1") != note:raise ValueError("Quality audit checkpoint differs")
    return digest
