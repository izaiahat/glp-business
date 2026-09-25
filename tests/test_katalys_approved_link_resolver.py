import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/katalys_approved_link_resolver.py"
SPEC = importlib.util.spec_from_file_location("katalys_approved_link_resolver", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


HEADERS = [
    "Priority", "Apply?", "Program / Network", "Category", "Network / Route",
    "Apply URL", "Official URL", "Fit Reason", "Suggested GLP Page / Asset",
    "SEO Slug", "Status", "Signup Complete?", "Publisher Login URL",
    "Affiliate / Publisher ID", "Tracking Link / Deep Link", "Coupon / Code",
    "Commission / Terms", "Cookie Window", "Approval Date", "Wire-In Ready?",
    "Wired Live?", "Link QA Status", "Compliance / Disclosure Notes",
    "Notes / Blocker", "Last Checked UTC", "Vertical", "API Capabilities",
]


def row(name, *, status="approved_active_not_wired", signup=True, link="", vertical="glp1-weightloss"):
    values = [""] * len(HEADERS)
    values[2] = name
    values[4] = "Katalys"
    values[5] = "https://app.katalys.com/pub/org/explore/123"
    values[10] = status
    values[11] = signup
    values[14] = link
    values[19] = False
    values[25] = vertical
    return values


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_approved_missing_link_is_selected_and_other_rows_are_not(tmp_path):
    backrun = tmp_path / "backrun"
    write_json(backrun / "sheet-full-row-export.json", {
        "headers": HEADERS,
        "rows": {
            "2": row("Ready target"),
            "3": row("Already linked", link="https://track.revoffers.com/x"),
            "4": row("Wrong vertical", vertical="general-wellness"),
            "5": row("Not approved", status="applied_pending_brand_approval"),
        },
    })
    write_json(backrun / "coverage-receipt.json", {
        "join": {"program_dispositions": [
            {"network": "katalys", "disposition": "MATCHED_TO_SHEET", "sheet_row": 2, "program_id": "123", "name": "Ready target"},
            {"network": "katalys", "disposition": "MATCHED_TO_SHEET", "sheet_row": 2, "program_id": "125", "name": "False brand-core collision"},
            {"network": "katalys", "disposition": "MATCHED_TO_SHEET", "sheet_row": 3, "program_id": "124", "name": "Already linked"},
        ]},
    })
    write_json(backrun / "marketplace/katalys-inventory.json", {
        "programs": [
            {"program_id": "123", "relationship": "approved"},
            {"program_id": "124", "relationship": "approved"},
            {"program_id": "125", "relationship": "approved"},
        ],
    })

    selected, blocked = MODULE.approved_unlinked_rows(backrun)

    assert blocked == []
    assert [(item["row"], item["program_id"], item["decision"]) for item in selected] == [
        (2, "123", "ALREADY_ACTIVE")
    ]


def test_approved_missing_link_with_nonapproved_provider_readback_is_loud(tmp_path):
    backrun = tmp_path / "backrun"
    write_json(backrun / "sheet-full-row-export.json", {"headers": HEADERS, "rows": {"2": row("Target")}})
    write_json(backrun / "coverage-receipt.json", {
        "join": {"program_dispositions": [
            {"network": "katalys", "disposition": "MATCHED_TO_SHEET", "sheet_row": 2, "program_id": "123", "name": "Target"},
        ]},
    })
    write_json(backrun / "marketplace/katalys-inventory.json", {
        "programs": [{"program_id": "123", "relationship": "pending"}],
    })

    selected, blocked = MODULE.approved_unlinked_rows(backrun)

    assert selected == []
    assert blocked[0]["reason"] == "current_katalys_relationship_not_approved:pending"


def test_plan_writes_readiness_only_for_provider_verified_links_and_isolates_failures():
    targets = [
        {"row": 2, "offer_id": 123, "program_id": "123", "name": "Good"},
        {"row": 3, "offer_id": 124, "program_id": "124", "name": "Bad"},
    ]
    tracking = {"programs": [
        {"offer_id": 123, "status": "GENERATED_VERIFIED", "tracking_link": "https://track.revoffers.com/a?offer_id=123&aff_id=13031"},
        {"offer_id": 124, "status": "BLOCKED_NO_CREATE_LINK"},
    ]}

    plan, blocked = MODULE.build_wire_plan(
        targets,
        tracking,
        authority_sha256="a" * 64,
        captured_at_utc="2026-08-16T00:00:00Z",
    )

    assert [item["row"] for item in plan["rows"]] == [2]
    assert plan["rows"][0]["cells"]["19"] == "TRUE"
    assert plan["rows"][0]["cells"]["10"] == "approved_active_tracking_ready"
    assert blocked == [{
        "row": 3,
        "name": "Bad",
        "program_id": "124",
        "reason": "BLOCKED_NO_CREATE_LINK",
        "detail": "",
    }]
