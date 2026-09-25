#!/usr/bin/env python3
"""Canonical typed schema enforcement for the GLP Affiliate Applications Sheet."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CHECKBOX_COLUMNS = {
    1: "Apply?",
    11: "Signup Complete?",
    19: "Wire-In Ready?",
    20: "Wired Live?",
}
DATE_COLUMN = 18
DATE_HEADER = "Approval Date"
DATE_NUMBER_FORMAT = {"type": "DATE", "pattern": "yyyy-mm-dd"}
VERTICAL_COLUMN = 25
VERTICAL_HEADER = "Vertical"
API_CAPABILITIES_COLUMN = 26
API_CAPABILITIES_HEADER = "API Capabilities"
VERTICAL_ALLOWED_VALUES = (
    "glp1-weightloss",
    "mens-health-ed",
    "mens-health-hair",
    "mens-health-trt",
    "peptides-longevity",
    "supplements-nutrition",
    "diagnostics-testing",
    "skincare-aesthetics",
    "fitness-equipment",
    "womens-health",
    "general-wellness",
    "out-of-scope",
)
EXPECTED_HEADERS = (
    "Priority", "Apply?", "Program / Network", "Category", "Network / Route", "Apply URL",
    "Official URL", "Fit Reason", "Suggested GLP Page / Asset", "SEO Slug", "Status",
    "Signup Complete?", "Publisher Login URL", "Affiliate / Publisher ID", "Tracking Link / Deep Link",
    "Coupon / Code", "Commission / Terms", "Cookie Window", "Approval Date", "Wire-In Ready?",
    "Wired Live?", "Link QA Status", "Compliance / Disclosure Notes", "Notes / Blocker", "Last Checked UTC",
    VERTICAL_HEADER, API_CAPABILITIES_HEADER,
)
EXPECTED_COLUMN_COUNT = len(EXPECTED_HEADERS)
EXPECTED_RANGE = "A:AA"
SHEETS_DATE_EPOCH = date(1899, 12, 30)

# Marketplace account/control rows are operational Sheet infrastructure, not
# disposable provider-program inventory.  Keep this immutable identity set in
# the canonical guard so every active/fallback writer that uses
# require_checkbox_schema_pass() fails closed if a parent is absent or cloned.
REQUIRED_MARKETPLACE_PARENT_NAMES = (
    "Impact.com",
    "CJ Affiliate",
    "Awin",
    "Partnerize",
    "FlexOffers",
    "Rakuten Advertising",
    "Katalys",
    "AvantLink",
    "Amazon Associates",
    "Walmart Affiliate Program",
    "Target Partners",
    "MarketHealth",
    "Sovrn Commerce",
    "Skimlinks",
    "Perform[cb]",
    "SellHealth",
    "Daisycon",
    "Webgains",
    "Tradedoubler",
    "Admitad",
    "MoreNiche",
    "MaxBounty",
    "HealthTrader",
    "Levanta",
    "Takeads",
    "Everflow Affiliate Marketplace",
    "ClickBank",
    "Digistore24",
    "PartnerStack Network",
    "Refersion Marketplace",
)

# These rows carry the marketplace-level login, publisher, URL, terms, notes,
# and API-access metadata used by the affiliate machine.  They are control
# records, not disposable programme rows.  Ordinary writers may read them but
# may not mutate or relocate them.  Only an explicitly parent-authorized
# migration carrier may change this map.
REQUIRED_MARKETPLACE_PARENT_ROWS = {
    "Impact.com": 777,
    "CJ Affiliate": 778,
    "Awin": 4,
    "Partnerize": 5,
    "FlexOffers": 6,
    "Rakuten Advertising": 7,
    "Katalys": 8,
    "AvantLink": 9,
    "Amazon Associates": 10,
    "Walmart Affiliate Program": 11,
    "Target Partners": 12,
    "MarketHealth": 425,
    "Sovrn Commerce": 426,
    "Skimlinks": 427,
    "Perform[cb]": 428,
    "SellHealth": 429,
    "Daisycon": 430,
    "Webgains": 431,
    "Tradedoubler": 432,
    "Admitad": 433,
    "MoreNiche": 434,
    "MaxBounty": 470,
    "HealthTrader": 347,
    "Levanta": 348,
    "Takeads": 354,
    "Everflow Affiliate Marketplace": 356,
    "ClickBank": 357,
    "Digistore24": 358,
    "PartnerStack Network": 359,
    "Refersion Marketplace": 360,
}
# Every parent has these persisted access/control fields. Some marketplaces
# truthfully have no separate apply/login endpoint or no API surface, so those
# optional blanks are not converted into invented data.
MARKETPLACE_PARENT_REQUIRED_METADATA_COLUMNS = (3, 6, 10, 22, 23, 24, 25)


def max_approval_date_serial() -> int:
    """Return today's Google Sheets date serial using the UTC business clock."""
    return (datetime.now(timezone.utc).date() - SHEETS_DATE_EPOCH).days


def column_letter(column: int) -> str:
    """Return an A1 column label for a zero-based column index."""
    if type(column) is not int or column < 0:
        raise ValueError(f"invalid_column_index:{column}")
    value = column + 1
    label = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def boolean_validation_requests(sheet_id: int, row_numbers: Iterable[int]) -> list[dict[str, Any]]:
    """Return explicit BOOLEAN data-validation requests for each target row/cell."""
    requests: list[dict[str, Any]] = []
    for row in sorted({int(value) for value in row_numbers}):
        if row < 2:
            raise ValueError(f"invalid_data_row:{row}")
        for column in CHECKBOX_COLUMNS:
            requests.append({
                "setDataValidation": {
                    "range": {
                        "sheetId": int(sheet_id),
                        "startRowIndex": row - 1,
                        "endRowIndex": row,
                        "startColumnIndex": column,
                        "endColumnIndex": column + 1,
                    },
                    "rule": {
                        "condition": {"type": "BOOLEAN"},
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            })
    return requests


def date_format_requests(sheet_id: int, row_numbers: Iterable[int]) -> list[dict[str, Any]]:
    """Return explicit canonical Approval Date formatting requests for target rows."""
    requests: list[dict[str, Any]] = []
    for row in sorted({int(value) for value in row_numbers}):
        if row < 2:
            raise ValueError(f"invalid_data_row:{row}")
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": int(sheet_id),
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": DATE_COLUMN,
                    "endColumnIndex": DATE_COLUMN + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": dict(DATE_NUMBER_FORMAT)}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    return requests


def vertical_validation_requests(sheet_id: int, row_numbers: Iterable[int]) -> list[dict[str, Any]]:
    """Return strict canonical Vertical dropdown validation for target rows."""
    requests: list[dict[str, Any]] = []
    for row in sorted({int(value) for value in row_numbers}):
        if row < 2:
            raise ValueError(f"invalid_data_row:{row}")
        requests.append({
            "setDataValidation": {
                "range": {
                    "sheetId": int(sheet_id),
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": VERTICAL_COLUMN,
                    "endColumnIndex": VERTICAL_COLUMN + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": value} for value in VERTICAL_ALLOWED_VALUES],
                    },
                    "strict": True,
                    "showCustomUi": True,
                },
            }
        })
    return requests


def schema_requests(sheet_id: int, row_numbers: Iterable[int]) -> list[dict[str, Any]]:
    """Return the complete immutable checkbox/date/Vertical schema for target rows."""
    rows = sorted({int(value) for value in row_numbers})
    return (
        boolean_validation_requests(sheet_id, rows)
        + date_format_requests(sheet_id, rows)
        + vertical_validation_requests(sheet_id, rows)
    )


def _canonical_values_hash(service, spreadsheet_id: str, tab: str) -> str:
    """Hash all Sheet values so schema-only repair cannot hide a value mutation."""
    values = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!{EXPECTED_RANGE}",
    ).execute().get("values", [])
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _repair_issue_contract(audit: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Return the stable issue identity used for the schema repair precondition."""
    issues = [
        (
            item.get("row"),
            item.get("column_index"),
            item.get("kind"),
            item.get("parent"),
            tuple(item.get("rows") or ()),
        )
        for item in audit.get("issues", [])
    ]
    # Parent-set failures have no single row/column while cell-schema failures
    # do.  JSON ordering avoids comparing None with int under Python 3 and the
    # parent/rows fields make an identity swap visible to CAS revalidation.
    return sorted(issues, key=lambda item: json.dumps(item, default=str))


def repair_non_value_schema_issues(service, spreadsheet_id: str, tab: str) -> dict[str, Any]:
    """Repair only validation/date-format drift with a value-preserving precondition."""
    before = audit_checkbox_schema(service, spreadsheet_id, tab)
    value_issue_kinds = {
        "header_mismatch",
        "checkbox_value_not_boolean",
        "approval_date_not_typed_number",
        "approval_date_number_out_of_range",
        "vertical_value_not_allowed",
        "api_capabilities_not_text",
        "required_marketplace_parent_missing",
        "required_marketplace_parent_duplicate",
    }
    nonrepairable = [item for item in before["issues"] if item.get("kind") in value_issue_kinds]
    if nonrepairable:
        raise RuntimeError(
            f"affiliate_sheet_typed_value_repair_required:{json.dumps(nonrepairable[:20], sort_keys=True)}"
        )
    repair_rows = sorted({int(item["row"]) for item in before["issues"] if item.get("row")})
    values_before_sha256 = _canonical_values_hash(service, spreadsheet_id, tab)

    # Re-read both the typed schema and all values immediately before the write.
    # A prior audit plus a later batchUpdate is not a compare-and-swap: another
    # writer could change a value or the target issue between those calls.
    pre_apply = audit_checkbox_schema(service, spreadsheet_id, tab)
    pre_apply_values_sha256 = _canonical_values_hash(service, spreadsheet_id, tab)
    if _repair_issue_contract(pre_apply) != _repair_issue_contract(before):
        raise RuntimeError("affiliate_sheet_schema_repair_precondition_changed")
    if pre_apply_values_sha256 != values_before_sha256:
        raise RuntimeError("affiliate_sheet_schema_repair_value_drift_before_apply")

    if repair_rows:
        metadata = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        ).execute()
        sheet_id = int(next(
            item["properties"]["sheetId"]
            for item in metadata["sheets"]
            if item["properties"]["title"] == tab
        ))
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": schema_requests(sheet_id, repair_rows)},
        ).execute()
    values_after_sha256 = _canonical_values_hash(service, spreadsheet_id, tab)
    if values_after_sha256 != values_before_sha256:
        raise RuntimeError("affiliate_sheet_schema_repair_changed_values")
    after = audit_checkbox_schema(service, spreadsheet_id, tab)
    if after["status"] != "PASS":
        raise RuntimeError(
            f"affiliate_sheet_schema_repair_failed:{json.dumps(after['issues'][:20], sort_keys=True)}"
        )
    return {
        "status": "PASS",
        "repair_rows": repair_rows,
        "repair_row_count": len(repair_rows),
        "values_before_sha256": values_before_sha256,
        "pre_apply_values_sha256": pre_apply_values_sha256,
        "precondition_revalidated": True,
        "values_after_sha256": values_after_sha256,
        "values_unchanged": True,
        "post_audit": after,
    }


def checkbox_cell_issues(cell: dict[str, Any], row: int, column: int) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    entered = cell.get("userEnteredValue") or {}
    rule = cell.get("dataValidation") or {}
    rule_type = ((rule.get("condition") or {}).get("type"))
    if set(entered) != {"boolValue"} or not isinstance(entered.get("boolValue"), bool):
        issues.append({
            "row": row,
            "column_index": column,
            "column": chr(ord("A") + column),
            "header": CHECKBOX_COLUMNS[column],
            "kind": "checkbox_value_not_boolean",
            "observed_type": next(iter(entered), "BLANK"),
        })
    if rule_type != "BOOLEAN":
        issues.append({
            "row": row,
            "column_index": column,
            "column": chr(ord("A") + column),
            "header": CHECKBOX_COLUMNS[column],
            "kind": "checkbox_validation_not_boolean",
            "observed_type": rule_type or "NONE",
        })
    if rule.get("strict") is not True:
        issues.append({
            "row": row, "column_index": column, "column": chr(ord("A") + column),
            "header": CHECKBOX_COLUMNS[column], "kind": "checkbox_validation_not_strict",
            "observed": rule.get("strict"),
        })
    return issues


def date_cell_issues(cell: dict[str, Any], row: int) -> list[dict[str, Any]]:
    """Require typed dates and canonical formatting on every populated row."""
    entered = cell.get("userEnteredValue") or {}
    number_format = ((cell.get("effectiveFormat") or {}).get("numberFormat") or {})
    issues: list[dict[str, Any]] = []
    if entered and (set(entered) != {"numberValue"} or type(entered.get("numberValue")) not in (int, float)):
        issues.append({
            "row": row,
            "column_index": DATE_COLUMN,
            "column": "S",
            "header": DATE_HEADER,
            "kind": "approval_date_not_typed_number",
            "observed_type": next(iter(entered), "BLANK"),
        })
    elif entered:
        number = entered["numberValue"]
        maximum = max_approval_date_serial()
        if not math.isfinite(number) or not 1 <= number <= maximum:
            issues.append({
                "row": row,
                "column_index": DATE_COLUMN,
                "column": "S",
                "header": DATE_HEADER,
                "kind": "approval_date_number_out_of_range",
                "observed": number,
                "minimum": 1,
                "maximum": maximum,
            })
    if number_format != DATE_NUMBER_FORMAT:
        issues.append({
            "row": row,
            "column_index": DATE_COLUMN,
            "column": "S",
            "header": DATE_HEADER,
            "kind": "approval_date_format_not_canonical",
            "observed": number_format,
            "expected": DATE_NUMBER_FORMAT,
        })
    return issues


def require_vertical_value(value: Any) -> str:
    """Return an allowlisted Vertical string or fail closed."""
    vertical = str(value or "").strip()
    if vertical not in VERTICAL_ALLOWED_VALUES:
        raise RuntimeError(f"affiliate_sheet_vertical_invalid:{vertical or 'BLANK'}")
    return vertical


def require_api_capabilities_value(value: Any) -> str:
    """Return an explicit text capability statement; blank is truthful and allowed."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuntimeError(f"affiliate_sheet_api_capabilities_not_text:{type(value).__name__}")
    return value.strip()


def vertical_cell_issues(cell: dict[str, Any], row: int) -> list[dict[str, Any]]:
    """Require an allowlisted Vertical value and the exact strict dropdown rule."""
    entered = cell.get("userEnteredValue") or {}
    rule = cell.get("dataValidation") or {}
    condition = rule.get("condition") or {}
    observed_values = [item.get("userEnteredValue") for item in condition.get("values") or []]
    issues: list[dict[str, Any]] = []
    if set(entered) != {"stringValue"} or entered.get("stringValue") not in VERTICAL_ALLOWED_VALUES:
        issues.append({
            "row": row,
            "column_index": VERTICAL_COLUMN,
            "column": "Z",
            "header": VERTICAL_HEADER,
            "kind": "vertical_value_not_allowed",
            "observed": entered.get("stringValue") if "stringValue" in entered else next(iter(entered), "BLANK"),
        })
    if condition.get("type") != "ONE_OF_LIST":
        issues.append({
            "row": row, "column_index": VERTICAL_COLUMN, "column": "Z", "header": VERTICAL_HEADER,
            "kind": "vertical_validation_not_one_of_list", "observed_type": condition.get("type") or "NONE",
        })
    if observed_values != list(VERTICAL_ALLOWED_VALUES):
        issues.append({
            "row": row, "column_index": VERTICAL_COLUMN, "column": "Z", "header": VERTICAL_HEADER,
            "kind": "vertical_validation_allowlist_mismatch", "observed": observed_values,
            "expected": list(VERTICAL_ALLOWED_VALUES),
        })
    if rule.get("strict") is not True:
        issues.append({
            "row": row, "column_index": VERTICAL_COLUMN, "column": "Z", "header": VERTICAL_HEADER,
            "kind": "vertical_validation_not_strict", "observed": rule.get("strict"),
        })
    if rule.get("showCustomUi") is not True:
        issues.append({
            "row": row, "column_index": VERTICAL_COLUMN, "column": "Z", "header": VERTICAL_HEADER,
            "kind": "vertical_validation_dropdown_hidden", "observed": rule.get("showCustomUi"),
        })
    return issues


def api_capabilities_cell_issues(cell: dict[str, Any], row: int) -> list[dict[str, Any]]:
    """Require API Capabilities to be blank or literal text, never a coerced type."""
    entered = cell.get("userEnteredValue") or {}
    if not entered or set(entered) == {"stringValue"}:
        return []
    return [{
        "row": row,
        "column_index": API_CAPABILITIES_COLUMN,
        "column": "AA",
        "header": API_CAPABILITIES_HEADER,
        "kind": "api_capabilities_not_text",
        "observed_type": next(iter(entered), "BLANK"),
    }]


def _cell(row_data: list[dict[str, Any]], row_number: int, column: int) -> dict[str, Any]:
    index = row_number - 1
    if index < 0 or index >= len(row_data):
        return {}
    values = row_data[index].get("values") or []
    return values[column] if column < len(values) else {}


def _cell_has_value(cell: dict[str, Any]) -> bool:
    entered = cell.get("userEnteredValue")
    if isinstance(entered, dict):
        for key in ("boolValue", "numberValue", "stringValue", "formulaValue"):
            if key in entered and (entered[key] not in (None, "") or entered[key] is False or entered[key] == 0):
                return True
    return bool(str(cell.get("formattedValue") or "").strip())


def _row_has_value(row_data: list[dict[str, Any]], row_number: int) -> bool:
    for column in range(EXPECTED_COLUMN_COUNT):
        cell = _cell(row_data, row_number, column)
        entered = cell.get("userEnteredValue") or {}
        if column in CHECKBOX_COLUMNS and entered.get("boolValue") is False:
            continue
        if _cell_has_value(cell):
            return True
    return False


def _header_issues(row_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observed = [
        ((_cell(row_data, 1, column).get("userEnteredValue") or {}).get("stringValue"))
        for column in range(EXPECTED_COLUMN_COUNT)
    ]
    if observed == list(EXPECTED_HEADERS):
        return []
    return [{"kind": "header_mismatch", "row": 1, "expected": list(EXPECTED_HEADERS), "observed": observed}]


def marketplace_parent_identity_issues_from_names(
    names_by_row: Iterable[tuple[int, Any]],
) -> list[dict[str, Any]]:
    """Return missing/duplicate required marketplace-parent identity issues.

    Identity is the exact trimmed ``Program / Network`` value.  Display-name
    aliases, row position, and network-route similarity are intentionally not
    accepted because they cannot prove that a required control record exists.
    """
    rows_by_name: dict[str, list[int]] = {name: [] for name in REQUIRED_MARKETPLACE_PARENT_NAMES}
    for row_number, raw_name in names_by_row:
        name = str(raw_name or "")
        if name in rows_by_name:
            rows_by_name[name].append(int(row_number))
    issues: list[dict[str, Any]] = []
    for name in REQUIRED_MARKETPLACE_PARENT_NAMES:
        rows = rows_by_name[name]
        if not rows:
            issues.append({"kind": "required_marketplace_parent_missing", "parent": name})
        elif len(rows) != 1:
            issues.append({
                "kind": "required_marketplace_parent_duplicate",
                "parent": name,
                "rows": sorted(rows),
                "count": len(rows),
            })
        elif rows[0] != REQUIRED_MARKETPLACE_PARENT_ROWS[name]:
            issues.append({
                "kind": "required_marketplace_parent_row_moved",
                "parent": name,
                "expected_row": REQUIRED_MARKETPLACE_PARENT_ROWS[name],
                "observed_row": rows[0],
            })
    return issues


def require_marketplace_parent_rows_not_mutated(
    mutations: Iterable[tuple[int, int]],
) -> None:
    """Reject ordinary-writer mutations to any marketplace control row."""
    protected_rows = frozenset(REQUIRED_MARKETPLACE_PARENT_ROWS.values())
    blocked = sorted({
        (int(row_number), int(column))
        for row_number, column in mutations
        if int(row_number) in protected_rows
    })
    if blocked:
        raise RuntimeError(
            f"required_marketplace_parent_row_mutation_blocked:{json.dumps(blocked)}"
        )


def marketplace_parent_identity_issues_from_values(values: Iterable[Iterable[Any]]) -> list[dict[str, Any]]:
    """Audit parent identities in primitive A:AA rows (header optional)."""
    names: list[tuple[int, Any]] = []
    for row_number, raw_row in enumerate(values, 1):
        row = list(raw_row)
        if row_number == 1 and row[:EXPECTED_COLUMN_COUNT] == list(EXPECTED_HEADERS):
            continue
        names.append((row_number, row[2] if len(row) > 2 else ""))
    return marketplace_parent_identity_issues_from_names(names)


def _marketplace_parent_identity_issues_from_grid(
    row_data: list[dict[str, Any]], row_count: int,
) -> list[dict[str, Any]]:
    names: list[tuple[int, Any]] = []
    for row_number in range(2, row_count + 1):
        cell = _cell(row_data, row_number, 2)
        entered = cell.get("userEnteredValue") or {}
        raw_name = entered.get("stringValue", cell.get("formattedValue", ""))
        names.append((row_number, raw_name))
    issues = marketplace_parent_identity_issues_from_names(names)
    for name, row_number in REQUIRED_MARKETPLACE_PARENT_ROWS.items():
        cell = _cell(row_data, row_number, 2)
        entered = cell.get("userEnteredValue") or {}
        observed_name = entered.get("stringValue", cell.get("formattedValue", ""))
        if observed_name != name:
            continue
        missing_columns = [
            column
            for column in MARKETPLACE_PARENT_REQUIRED_METADATA_COLUMNS
            if not _cell_has_value(_cell(row_data, row_number, column))
        ]
        if missing_columns:
            issues.append({
                "kind": "required_marketplace_parent_operational_metadata_missing",
                "parent": name,
                "row": row_number,
                "columns": missing_columns,
            })
    return issues


def audit_checkbox_schema(service, spreadsheet_id: str, tab: str) -> dict[str, Any]:
    """Audit the complete A:AA typed contract across every populated data row."""
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))",
    ).execute()
    properties = next(item["properties"] for item in metadata["sheets"] if item["properties"]["title"] == tab)
    row_count = int(properties["gridProperties"]["rowCount"])
    column_count = int(properties["gridProperties"]["columnCount"])
    if column_count != EXPECTED_COLUMN_COUNT:
        return {
            "status": "FAIL",
            "row_count": row_count,
            "column_count": column_count,
            "populated_rows_checked": 0,
            "checkbox_cells_checked": 0,
            "issues": [{"kind": "column_count_mismatch", "expected": EXPECTED_COLUMN_COUNT, "observed": column_count}],
        }
    response = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        ranges=[f"'{tab}'!A1:AA{row_count}"],
        includeGridData=True,
        fields="sheets(data(startRow,rowData(values(userEnteredValue,dataValidation,formattedValue,effectiveFormat(numberFormat)))))",
    ).execute()
    block = response["sheets"][0]["data"][0]
    start_row_index = int(block.get("startRow", 0))
    row_data = block.get("rowData") or []
    if start_row_index != 0:
        return {
            "status": "FAIL",
            "row_count": row_count,
            "column_count": column_count,
            "populated_rows_checked": 0,
            "checkbox_cells_checked": 0,
            "issues": [{"kind": "unexpected_grid_start", "observed": start_row_index}],
        }
    populated_rows: list[int] = []
    issues: list[dict[str, Any]] = _header_issues(row_data)
    issues.extend(_marketplace_parent_identity_issues_from_grid(row_data, row_count))
    for row_number in range(2, row_count + 1):
        if not _row_has_value(row_data, row_number):
            continue
        populated_rows.append(row_number)
        for column in CHECKBOX_COLUMNS:
            issues.extend(checkbox_cell_issues(_cell(row_data, row_number, column), row_number, column))
        issues.extend(date_cell_issues(_cell(row_data, row_number, DATE_COLUMN), row_number))
        issues.extend(vertical_cell_issues(_cell(row_data, row_number, VERTICAL_COLUMN), row_number))
        issues.extend(api_capabilities_cell_issues(_cell(row_data, row_number, API_CAPABILITIES_COLUMN), row_number))
    return {
        "status": "PASS" if not issues else "FAIL",
        "row_count": row_count,
        "column_count": column_count,
        "populated_rows_checked": len(populated_rows),
        "checkbox_cells_checked": len(populated_rows) * len(CHECKBOX_COLUMNS),
        "approval_date_cells_checked": len(populated_rows),
        "vertical_cells_checked": len(populated_rows),
        "api_capabilities_cells_checked": len(populated_rows),
        "first_populated_row": min(populated_rows) if populated_rows else None,
        "last_populated_row": max(populated_rows) if populated_rows else None,
        "issues": issues,
    }


def require_checkbox_schema_pass(service, spreadsheet_id: str, tab: str) -> dict[str, Any]:
    result = audit_checkbox_schema(service, spreadsheet_id, tab)
    if result["status"] != "PASS":
        raise RuntimeError(f"affiliate_sheet_checkbox_schema_regression:{json.dumps(result['issues'][:20], sort_keys=True)}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True, help="Private GridData fixture for offline verification")
    args = parser.parse_args()
    payload = json.loads(args.snapshot.read_text())
    rows = ((payload["sheets"][0].get("data") or [{}])[0].get("rowData") or [])
    issues = _header_issues(rows)
    issues.extend(_marketplace_parent_identity_issues_from_grid(rows, len(rows)))
    populated = 0
    for row_number in range(2, len(rows) + 1):
        values = rows[row_number - 1].get("values") or []
        if not _row_has_value(rows, row_number):
            continue
        populated += 1
        for column in CHECKBOX_COLUMNS:
            cell = values[column] if column < len(values) else {}
            issues.extend(checkbox_cell_issues(cell, row_number, column))
        date_cell = values[DATE_COLUMN] if DATE_COLUMN < len(values) else {}
        issues.extend(date_cell_issues(date_cell, row_number))
        vertical_cell = values[VERTICAL_COLUMN] if VERTICAL_COLUMN < len(values) else {}
        issues.extend(vertical_cell_issues(vertical_cell, row_number))
        api_cell = values[API_CAPABILITIES_COLUMN] if API_CAPABILITIES_COLUMN < len(values) else {}
        issues.extend(api_capabilities_cell_issues(api_cell, row_number))
    result = {
        "status": "PASS" if not issues else "FAIL",
        "populated_rows_checked": populated,
        "checkbox_cells_checked": populated * len(CHECKBOX_COLUMNS),
        "approval_date_cells_checked": populated,
        "vertical_cells_checked": populated,
        "api_capabilities_cells_checked": populated,
        "issues": issues,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
