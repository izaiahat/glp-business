"""GLP-339: every F01-F13 pipeline publishes a complete applicable-tools contract."""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "ops/pipeline-tool-contracts.json"
PIPELINES = [f"F{i:02d}" for i in range(1, 14)]
REQUIRED = {
    "wired": ("consumer", "job", "readback_artifact", "evidence"),
    "hold": ("owner", "reason"),
    "gap": ("owner", "reason"),
    "not_applicable": ("reason",),
}


def _contract():
    return json.loads(CONTRACT.read_text())


def test_pipeline_tool_contracts_are_complete_and_wired_consumers_exist():
    contract = _contract()
    assert contract["schema"] == "glp-pipeline-tool-contracts-v1"
    universe = set(contract["tool_universe"])
    assert universe and "fella_health" in contract["permanent_exclusions"]
    rows = contract["pipelines"]
    assert list(rows) == PIPELINES
    problems = []
    for pid in PIPELINES:
        row = rows[pid]
        assert row.get("definition_status") in ("defined", "UNKNOWN"), pid
        tools = row["tools"]
        # The full denominator: no applicable tool may be silently omitted.
        if set(tools) != universe:
            problems.append((pid, "tool_set", sorted(universe ^ set(tools))))
        for tool, entry in tools.items():
            status = entry.get("status")
            if status not in REQUIRED:
                problems.append((pid, tool, "invalid_status", status))
                continue
            for field in REQUIRED[status]:
                value = entry.get(field)
                if not isinstance(value, str) or not value.strip():
                    problems.append((pid, tool, "missing", field))
            if status == "wired":
                consumer = entry.get("consumer") or ""
                if Path(consumer).is_absolute() or not (REPO / consumer).is_file():
                    problems.append((pid, tool, "consumer_missing", consumer))
            if tool in contract["permanent_exclusions"] and status != "not_applicable":
                problems.append((pid, tool, "excluded_provider_not_excluded", status))
        if row["definition_status"] == "UNKNOWN":
            # Undefined rows may not claim wiring or inapplicability they cannot prove.
            claimed = {t for t, e in tools.items()
                       if t not in contract["permanent_exclusions"] and e.get("status") != "gap"}
            if claimed:
                problems.append((pid, "unknown_row_claims", sorted(claimed)))
    assert problems == []
