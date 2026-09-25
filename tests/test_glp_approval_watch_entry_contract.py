"""Regression guard for approval-watch execution 3945's bridge/parser mismatch.

This test stops immediately after argparse. It never reads provider state, writes
artifacts, or inserts single-writer queue rows.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BUSINESS = Path("/home/ubuntu/business")
sys.path[:0] = [str(BUSINESS / "scripts"), str(BUSINESS)]


def _load_watch():
    spec = importlib.util.spec_from_file_location(
        "approval_watch_entry_contract", BUSINESS / "scripts/glp_approval_watch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WATCH = _load_watch()

def _registered_command(args):
    spec = importlib.util.spec_from_file_location(
        "approval_watch_bridge_contract", BUSINESS / "ops/n8n-bridge/bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    actions = json.loads((BUSINESS / "ops/n8n-bridge/actions.json").read_text())
    return bridge.build_script_command(
        {"action": "script", "name": "approval_watch", "args": args}, actions)["argv"]


class ParsedOnly(RuntimeError):
    def __init__(self, namespace):
        self.namespace = namespace
        super().__init__("parsed_only")


@pytest.mark.parametrize(
    ("argv", "expected_queue"),
    [
        (["--queue"], "1"),
        (["--queue", "1"], "1"),
        (["--queue", "false"], "false"),
    ],
)
def test_queue_accepts_cli_and_registered_bridge_shapes_without_running_machine(
        monkeypatch, argv, expected_queue):
    """The bridge emits ``--queue 1``; argparse must also retain bare CLI use."""
    original_parse_args = WATCH.argparse.ArgumentParser.parse_args

    def parse_then_stop(parser):
        namespace = original_parse_args(parser, argv)
        raise ParsedOnly(namespace)

    monkeypatch.setattr(WATCH.argparse.ArgumentParser, "parse_args", parse_then_stop)
    with pytest.raises(ParsedOnly) as excinfo:
        WATCH.main()

    assert excinfo.value.namespace.queue == expected_queue


@pytest.mark.parametrize(
    ("bridge_args", "expected_coverage", "expected_queue"),
    [
        ({"coverage-only": "1"}, True, None),
        ({"coverage-only": "0"}, False, None),
        ({"queue": "1"}, False, "1"),
        ({}, False, None),
    ],
)
def test_registered_approval_watch_flags_parse_without_running_machine(
        monkeypatch, bridge_args, expected_coverage, expected_queue):
    """Bridge flag translation must preserve coverage-only and omitted live modes."""
    argv = _registered_command(bridge_args)
    assert argv[:2] == ["python3", "scripts/glp_approval_watch.py"]
    original_parse_args = WATCH.argparse.ArgumentParser.parse_args

    def parse_then_stop(parser):
        namespace = original_parse_args(parser, argv[2:])
        raise ParsedOnly(namespace)

    monkeypatch.setattr(WATCH.argparse.ArgumentParser, "parse_args", parse_then_stop)
    with pytest.raises(ParsedOnly) as excinfo:
        WATCH.main()

    assert excinfo.value.namespace.coverage_only is expected_coverage
    assert excinfo.value.namespace.queue == expected_queue
