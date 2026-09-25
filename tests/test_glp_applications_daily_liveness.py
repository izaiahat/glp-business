from __future__ import annotations

import copy

import pytest

from scripts import repair_glp_applications_daily_liveness as repair


def workflow_fixture() -> dict:
    return {
        "id": repair.WORKFLOW_ID,
        "name": repair.WORKFLOW_NAME,
        "active": True,
        "isArchived": False,
        "nodes": [
            {
                "id": "t2",
                "name": repair.WEBHOOK_NAME,
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [-460, 140],
                "parameters": {
                    "path": repair.WEBHOOK_PATH,
                    "httpMethod": "POST",
                    "responseMode": "onReceived",
                },
            },
            {"id": "config", "name": "Config", "type": "n8n-nodes-base.code",
             "parameters": {}},
            {"id": "run", "name": "Run Applications",
             "type": "n8n-nodes-base.httpRequest", "parameters": {}},
            {"id": "parse", "name": "Parse Applications",
             "type": "n8n-nodes-base.code", "parameters": {}},
            {"id": "collapse", "name": "Collapse Applications",
             "type": "n8n-nodes-base.code", "parameters": {}},
            {"id": "hb", "name": "HB applications",
             "type": "n8n-nodes-base.dataTable", "parameters": {}},
        ],
        "connections": {
            repair.WEBHOOK_NAME: {
                "main": [[{"node": "Config", "type": "main", "index": 0}]]
            },
            "Config": {
                "main": [[{"node": "Run Applications", "type": "main", "index": 0}]]
            },
        },
        "settings": {"timezone": repair.TIMEZONE, "executionOrder": "v1"},
        "staticData": {
            "node:Schedule 0940 PT": {
                "recurrenceRules": [None],
                "recurrenceRuleSignatures": [None],
            }
        },
        "pinData": None,
        "nodeGroups": [],
    }


def strip_repair_delta(workflow: dict) -> dict:
    stripped = copy.deepcopy(workflow)
    stripped["nodes"] = [
        node for node in stripped["nodes"] if node.get("name") != repair.SCHEDULE_NAME
    ]
    stripped["connections"].pop(repair.SCHEDULE_NAME, None)
    return stripped


def test_adds_only_the_registered_daily_schedule_and_connection():
    before = workflow_fixture()
    frozen = copy.deepcopy(before)
    candidate, changed = repair.install_daily_liveness_schedule(before)

    assert changed is True
    assert before == frozen
    assert strip_repair_delta(candidate) == before
    repair.validate_repaired_contract(candidate)
    schedule = repair.schedule_nodes(candidate)[0]
    assert schedule["parameters"]["rule"]["interval"] == [
        {"field": "cronExpression", "expression": "40 9 * * *"}
    ]
    assert repair._trigger_target(candidate, repair.SCHEDULE_NAME) == "Config"
    assert repair._trigger_target(candidate, repair.WEBHOOK_NAME) == "Config"


def test_exact_repair_is_idempotent():
    once, changed = repair.install_daily_liveness_schedule(workflow_fixture())
    assert changed is True
    twice, changed_again = repair.install_daily_liveness_schedule(once)
    assert changed_again is False
    assert twice == once


def test_unknown_schedule_fails_closed_instead_of_replacing_it():
    workflow = workflow_fixture()
    workflow["nodes"].insert(0, {
        "id": "other", "name": "Unexpected Hourly Poll",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
        "parameters": {"rule": {"interval": [
            {"field": "cronExpression", "expression": "0 * * * *"}
        ]}},
    })
    with pytest.raises(repair.ContractError, match="unknown or malformed schedule"):
        repair.install_daily_liveness_schedule(workflow)


def test_webhook_must_remain_registered_and_bound_to_config():
    workflow = workflow_fixture()
    workflow["connections"][repair.WEBHOOK_NAME]["main"][0][0]["node"] = "Elsewhere"
    with pytest.raises(repair.ContractError, match="not bound to Config"):
        repair.install_daily_liveness_schedule(workflow)


def test_update_payload_excludes_read_only_activation_and_version_fields():
    workflow = workflow_fixture()
    workflow.update({"activeVersionId": "published", "versionId": "draft",
                     "createdAt": "never-send", "updatedAt": "never-send",
                     "description": None})
    payload = repair.update_payload(workflow)
    assert set(payload) == {
        "name", "nodes", "connections", "settings", "nodeGroups",
        "staticData", "pinData",
    }
    assert "active" not in payload
    assert "versionId" not in payload
    assert "activeVersionId" not in payload
    assert "description" not in payload


def test_update_payload_keeps_a_real_description():
    workflow = workflow_fixture()
    workflow["description"] = "Daily gated applications liveness"
    assert repair.update_payload(workflow)["description"] == workflow["description"]
