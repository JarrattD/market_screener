"""Publish one current ranking to the separate, private UK & CI workbook."""
from __future__ import annotations

from datetime import date
import math
import re
from urllib.parse import urlparse

TITLE = "UK & Channel Islands Ranked"
MARKER = "uk-ci-ranked-v1"
RATIO_KEYS = ("p_ncav", "p_tbv", "p_fcf_5y", "ev_fcf_5y", "p_fcf_latest", "ev_fcf_latest")
RANKED = (
    "Rank", "Score /7", "Company", "Ticker", "Market", "ISIN", "Type",
    "HQ country", "HQ evidence", "Financial points /6", "Ownership point",
    "Ownership band", "P/NCAV", "P/TBV", "P/5Y FCF", "EV/5Y FCF",
    "P/latest FY FCF", "EV/latest FY FCF", "FMP symbol", "Data status",
    "Financial data notes", "Listing source",
)
REVIEW = (
    "Ticker", "Company", "Market", "ISIN", "Type", "HQ country",
    "HQ evidence", "FMP symbol", "Listing source",
)
COVERAGE = ("Metric", "Value")
EXPECTED = {"Ranked": "Rank", "HQ Review": "Ticker", "Coverage": "Metric"}
COLOURS = {
    "green": {"red": .81, "green": .91, "blue": .83},
    "orange": {"red": .99, "green": .89, "blue": .72},
    "yellow": {"red": 1., "green": .95, "blue": .75},
    "grey": {"red": .92, "green": .93, "blue": .94},
    "gray": {"red": .92, "green": .93, "blue": .94},
}


def open_destination():
    """Use only the existing service account, exact title and non-secret marker."""
    import gspread

    client = gspread.service_account(filename="private_uk_ci/service_account.json")
    files = client.list_spreadsheet_files(title=TITLE)
    matches = [file for file in files if file.get("name") == TITLE]
    if len(matches) != 1:
        raise ValueError("The existing writer must see exactly one dedicated UK & CI workbook")
    sheet = client.open_by_key(matches[0]["id"])
    if sheet.title != TITLE:
        raise ValueError("Unexpected workbook title")
    return sheet


def _tabs(sheet):
    tabs = {tab.title: tab for tab in sheet.worksheets()}
    if not EXPECTED.keys() <= tabs.keys():
        raise ValueError("Missing required UK & CI workbook tabs")
    if any(tabs[name].acell("A1").value != header for name, header in EXPECTED.items()):
        raise ValueError("Unexpected UK & CI workbook headers")
    if (tabs["Coverage"].acell("A2").value != "Template"
            or tabs["Coverage"].acell("B2").value != MARKER):
        raise ValueError("Destination workbook identity was not verified")
    return tabs


def verify_destination(sheet):
    _tabs(sheet)


def parse_ownership(rows, today=None):
    """Only manually verified, dated evidence; reject duplicate/conflicting latest entries."""
    today = today or date.today()
    result, conflicts = {}, set()
    for row in rows:
        if len(row) < 6:
            continue
        isin, ticker, raw_pct, raw_date, source, holder = (str(v).strip() for v in row[:6])
        isin, ticker = isin.upper(), ticker.upper()
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) or not ticker or not holder:
            continue
        try:
            day = date.fromisoformat(raw_date)
            pct = float(raw_pct)
            url = urlparse(source)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                continue
        except (ValueError, TypeError):
            continue
        if not (0 < pct <= 100 and 0 <= (today - day).days <= 400):
            continue
        evidence = {"ticker": ticker, "pct": pct, "as_of": day.isoformat(),
                    "source": source, "holder": holder}
        old = result.get(isin)
        if old is None or old["as_of"] < evidence["as_of"]:
            result[isin] = evidence
            conflicts.discard(isin)
        elif old["as_of"] == evidence["as_of"] and old != evidence:
            conflicts.add(isin)
    return {isin: row for isin, row in result.items() if isin not in conflicts}


def read_ownership(sheet):
    tab = next((tab for tab in sheet.worksheets() if tab.title == "Ownership Evidence"), None)
    if tab is None:
        return {}
    rows = tab.get("A1:G", value_render_option="FORMATTED_VALUE")
    if not rows or rows[0][:2] != ["ISIN", "Ticker (market)"]:
        raise ValueError("Unexpected Ownership Evidence headers")
    return parse_ownership(rows[1:])


def _cell(value, band=None, heading=False, column_format=None):
    cell = {}
    if isinstance(value, bool):
        cell["userEnteredValue"] = {"boolValue": value}
    elif isinstance(value, (int, float)) and math.isfinite(value):
        cell["userEnteredValue"] = {"numberValue": value}
    elif isinstance(value, str) and value:
        # stringValue, not formulaValue: issuer names and evidence are untrusted text.
        cell["userEnteredValue"] = {"stringValue": value}
    if heading:
        cell["userEnteredFormat"] = {
            "backgroundColor": {"red": .09, "green": .17, "blue": .28},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "verticalAlignment": "MIDDLE",
        }
    else:
        fmt = {}
        if band in COLOURS:
            fmt["backgroundColor"] = COLOURS[band]
        if column_format and "numberValue" in cell.get("userEnteredValue", {}):
            fmt["numberFormat"] = {"type": "NUMBER", "pattern": column_format}
        if fmt:
            cell["userEnteredFormat"] = fmt
    return cell


def sheet_requests(tabs, datasets, bands):
    requests = []
    for name, matrix in datasets.items():
        tab = tabs[name]
        width = len(matrix[0])
        height = len(matrix)
        row_count = max(height + 20, tab.row_count, 100)
        col_count = max(width, tab.col_count)
        requests.append({"updateSheetProperties": {"properties": {"sheetId": tab.id,
                            "gridProperties": {"rowCount": row_count, "columnCount": col_count,
                                               "frozenRowCount": 1}},
                         "fields": "gridProperties(rowCount,columnCount,frozenRowCount)"}})
        rows = []
        for i, row in enumerate(matrix):
            fills = bands[i - 1] if i and name == "Ranked" else {}
            formats = ({1: '0"/7"', 9: '0"/6"', **{n: "0.00" for n in range(12, 18)}}
                       if name == "Ranked" else {})
            rows.append({"values": [_cell(v, fills.get(j), i == 0, formats.get(j))
                                    for j, v in enumerate(row)]})
        # A single atomic request clears the previous generated rows and writes the new ones.
        grid = {"sheetId": tab.id, "startRowIndex": 0, "endRowIndex": row_count,
                "startColumnIndex": 0, "endColumnIndex": width}
        requests.append({"updateCells": {"range": grid, "rows": rows,
                                          "fields": "userEnteredValue,userEnteredFormat"}})
        if name in ("Ranked", "HQ Review"):
            requests.append({"setBasicFilter": {"filter": {"range": {**grid, "endRowIndex": height}}}})
        widths = ([62, 85, 260, 95, 115, 145, 135, 95, 350, 135, 125, 130,
                   90, 90, 100, 100, 125, 125, 100, 155, 425, 240] if name == "Ranked"
                  else ([105, 260, 115, 145, 135, 95, 420, 100, 240] if name == "HQ Review"
                        else [295, 585]))
        for j, pixels in enumerate(widths):
            requests.append({"updateDimensionProperties": {"range": {"sheetId": tab.id,
                             "dimension": "COLUMNS", "startIndex": j, "endIndex": j + 1},
                             "properties": {"pixelSize": pixels}, "fields": "pixelSize"}})
    return requests


def _matches(actual, expected):
    if len(actual) > len(expected):
        return False
    for i, row in enumerate(expected):
        given = actual[i] if i < len(actual) else []
        if len(given) > len(row):
            return False
        for j, target in enumerate(row):
            found = given[j] if j < len(given) else ""
            if isinstance(target, (int, float)) and not isinstance(target, bool):
                if not isinstance(found, (int, float)) or isinstance(found, bool):
                    return False
                if not math.isclose(found, target, rel_tol=1e-12, abs_tol=1e-12):
                    return False
            elif (target if target is not None else "") != found:
                return False
    return True


def publish(sheet, ranked_rows, review_rows, coverage_rows, bands):
    from gspread.utils import rowcol_to_a1

    tabs = _tabs(sheet)
    datasets = {
        "Ranked": [RANKED, *ranked_rows],
        "HQ Review": [REVIEW, *review_rows],
        "Coverage": [COVERAGE, ("Template", MARKER), *coverage_rows],
    }
    if len(ranked_rows) != len(bands) or any(
        len(matrix[0]) != len(row) for matrix in datasets.values() for row in matrix
    ):
        raise ValueError("Unexpected output matrix dimensions")
    sheet.batch_update({"requests": sheet_requests(tabs, datasets, bands)})
    for name, rows in datasets.items():
        end = rowcol_to_a1(len(rows), len(rows[0]))
        actual = tabs[name].get(f"A1:{end}", value_render_option="UNFORMATTED_VALUE")
        if not _matches(actual, rows):
            raise RuntimeError("Published worksheet read-back failed")
