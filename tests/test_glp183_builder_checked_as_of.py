"""Offline exact-source builder/caller regressions; no private inputs or effects."""
import argparse
import ast
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace

import pytest


def load_contract_functions():
    """Compile unchanged checkout AST bodies, avoiding omitted host-only imports.

    main and apply_changes are real caller bodies. Unrelated external authority
    validation is fixture-bound in the CLI test; all write boundaries are traps.
    This is isolated caller coverage, not full-module/site-build portability.
    """
    source = Path(__file__).resolve().parents[1] / "scripts/build_glp_affiliate_wirein_incremental_v2.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    names = {"quote", "stated", "canonical_field_sources", "normalize_regulatory_banner",
             "build_contract", "validate_checked_as_of", "apply_changes", "main",
             "read_json", "write_json", "file_sha", "sha_bytes", "logo_mutation_paths"}
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    # Permit predecessor loading for explicit red runs: it lacks the new helper.
    assert {node.name for node in functions} >= names - {"validate_checked_as_of"}
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "COMPOUNDED_TAIL"
                         for target in node.targets)]
    assert len(constants) == 1
    namespace = dict(Path=Path, datetime=datetime, re=re, hashlib=hashlib,
                     json=json, os=os, argparse=argparse, sys=sys, shutil=shutil)
    exec(compile(ast.Module(body=[*constants, *functions], type_ignores=[]), str(source), "exec"),
         namespace)
    return SimpleNamespace(**namespace)


builder = load_contract_functions()
MISSING = object()
BAD_DATES = [MISSING, None, "", "   ", "\t\r\n", "today", "2026-9-1",
             "31-08-2026", "2026-08-31T00:00:00Z", "2026-13-01", "2026-02-30",
             True, 20260831, ["2026-08-31"], "20260831", "2026-W36-1",
             "2026-08-31\n", " 2026-08-31", "0000-01-01"]


def inputs():
    """Complete synthetic contract inputs, independent of private provider data."""
    spec = {
        "program_id": "fixture-provider", "display_name": "Fixture Provider",
        "official_url": "https://provider.example/", "advertiser_id": "fixture-offer",
        "network": "Awin", "logo_output_file": "fixture.webp",
        "checked_as_of": "2026-08-31", "regulatory_banner_required": False,
        "research_file": "research.json",
        "page_copy": {"title": "Fixture", "description": "Fixture description",
                      "bottom_line": "Fixture bottom line"},
        "row_contract": {"best_for": "Fixture", "cons": [], "insurance": "Unknown",
                         "medications": [], "pros": [], "starting_price": "Unknown"},
    }
    research = {"schema": "program-deep-research-v1", "fields": {
        name: {"value": "Fixture source text", "source_url": "https://provider.example/terms",
               "verified_as_of": "2026-08-31"}
        for name in ("first_month_price", "states_served", "consultation_fee",
                     "cancellation", "pharmacy_type", "shipping_wait")
    }}
    logo = {"path": "/fixture.webp", "source_asset_url": "https://provider.example/logo.webp",
            "sha256": "0" * 64}
    return spec, research, logo


def with_date(spec, value):
    spec = deepcopy(spec)
    if value is MISSING:
        spec.pop("checked_as_of")
    else:
        spec["checked_as_of"] = value
    return spec


def build(spec, research, logo):
    return builder.build_contract(spec, research, "https://tracking.example/private-token", logo,
                                  "private-publisher")


@pytest.mark.parametrize("value", BAD_DATES)
def test_bad_date_is_named_private_value_free_refusal(value):
    spec, research, logo = inputs()
    spec = with_date(spec, value)
    before = deepcopy((spec, research, logo))
    reason = "missing" if value is MISSING or value is None or (isinstance(value, str) and not value.strip()) else "invalid"
    with pytest.raises(RuntimeError) as error:
        build(spec, research, logo)
    assert str(error.value) == f"spec_checked_as_of_{reason}:record=fixture-provider"
    assert (spec, research, logo) == before


def test_errors_distinguish_records_and_redact_unexpected_identifier_payload():
    spec, research, logo = inputs()
    spec["checked_as_of"] = "https://tracking.example/private-date"
    messages = []
    for pid in ["fixture-alpha", "fixture-beta", "https://tracking.example/private-identifier"]:
        spec["program_id"] = pid
        with pytest.raises(RuntimeError) as error:
            build(spec, research, logo)
        message = str(error.value)
        assert "checked_as_of" in message
        assert "private" not in message and "https://" not in message
        assert pid in message if pid.startswith("fixture-") else "record=sha256-" in message
        messages.append(message)
    assert len(set(messages)) == 3


@pytest.mark.parametrize("date", ["2026-08-31", "2024-02-29", "2000-01-01", "2026-12-31"])
def test_valid_source_date_is_preserved_without_mutating_inputs(date):
    spec, research, logo = inputs()
    spec["checked_as_of"] = date
    before = deepcopy((spec, research, logo))
    result = build(spec, research, logo)
    assert result["network_contract"]["local_evidence_checked_through"] == date
    assert result["program_id"] == spec["program_id"]
    assert result["page_copy"]["pricing"] == research["fields"]["first_month_price"]["value"]
    assert (spec, research, logo) == before


def test_64_valid_contracts_match_predecessor_serialized_bytes():
    # Golden aggregate from unchanged build_contract at 540d92e82140ccb4891291973057548ca2ad40a2.
    # Four dates x four banner forms x four optional-data branches; no Git history
    # dependency at test time (CI uses a shallow checkout).
    digest = hashlib.sha256()
    count = 0
    for date in ["2026-08-31", "2024-02-29", "2000-01-01", "2026-12-31"]:
        for banner in [True, False, "compounded_glp_general", "no_compounded_classification_published"]:
            for variant in ["default", "tracking_host", "not_publicly_stated", "supplied_sources"]:
                spec, research, logo = inputs()
                spec.update(checked_as_of=date, regulatory_banner_required=banner)
                if variant == "tracking_host":
                    spec["tracking_host"] = "tracking.example"
                if variant == "not_publicly_stated":
                    for field in ["states_served", "consultation_fee", "cancellation", "pharmacy_type", "shipping_wait"]:
                        research["fields"][field].update(value=None, state="not_publicly_stated")
                if variant == "supplied_sources":
                    spec["field_sources"] = {"program_terms": ["https://provider.example/extra"]}
                before = deepcopy((spec, research, logo))
                result = build(spec, research, logo)
                digest.update((json.dumps(result, indent=2, sort_keys=True) + "\n").encode())
                assert (spec, research, logo) == before
                count += 1
    assert count == 64
    assert digest.hexdigest() == "3cb0ae2c864d2e38a137079756ebcfb790c546a7c317274c495899802a6e042b"


class MutationReached(BaseException):
    """Bypass exception cleanup too: no create-then-delete can mask a write."""


def trap_mutations(monkeypatch, namespace):
    calls = []

    def trap(*args, **kwargs):
        calls.append("mutation")
        raise MutationReached()

    for name in ["write_json", "copytree_linked", "detach_declared_mutations",
                 "stage_bound_logos", "remove_materialized_tree"]:
        monkeypatch.setitem(namespace, name, trap)
    # Also trap direct filesystem mutations, not just the known helper calls.
    for name in ["mkdir", "write_text", "write_bytes", "unlink", "rename", "replace", "touch"]:
        monkeypatch.setattr(Path, name, trap)
    real_open = Path.open

    def guarded_open(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            return trap()
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    return calls


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("value", BAD_DATES + ["2026-08-31"])
def test_actual_writer_batch_refuses_before_first_write(tmp_path, monkeypatch, value):
    ns = builder.apply_changes.__globals__
    spec, research, logo = inputs()
    prior = dict(spec, program_id="fixture-prior")
    specs = [prior, with_date(spec, value)]
    target, research_dir = tmp_path / "target", tmp_path / "research"
    asset = target / "assets/provider-logos/fixture.webp"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"synthetic-logo")
    builder.write_json(asset.parent / "manifest.json", {"logos": [dict(
        program_id=s["program_id"], output_sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),
        public_path="/fixture.webp", width=1, height=1, media_type="image/webp", surface="light",
        source_asset_url=logo["source_asset_url"]) for s in specs]})
    builder.write_json(target / "data/provider-page-contracts.json",
                       {"providers": [], "target_release": {"requested_provider_review_count": 0}})
    builder.write_json(research_dir / "research.json", research)
    before = snapshot(tmp_path)
    with monkeypatch.context() as guarded:
        calls = trap_mutations(guarded, ns)
        expected = MutationReached if value == "2026-08-31" else RuntimeError
        with pytest.raises(expected) as error:
            builder.apply_changes(target, specs,
                {s["program_id"]: "https://tracking.example/private" for s in specs},
                {s["program_id"]: "private-publisher" for s in specs},
                "2026-08-31T00:00:00Z", tmp_path / "sheet.json", research_dir)
        if expected is RuntimeError:
            assert "checked_as_of" in str(error.value) and "fixture-provider" in str(error.value)
            assert calls == []
        else:
            assert calls == ["mutation"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("value", BAD_DATES + ["2026-08-31"])
def test_actual_cli_caller_refuses_before_materialization_or_cleanup(tmp_path, monkeypatch, value):
    ns = builder.main.__globals__
    source, research_dir, output = (tmp_path / name for name in ("source", "research", "output"))
    for directory in [source, research_dir, output]:
        directory.mkdir()
    (output / "untouched.txt").write_bytes(b"retain exact bytes and mtime")
    spec, _, _ = inputs()
    spec.update(network="Direct", sheet_row=1, publisher_id="private-publisher")
    prior = dict(spec, program_id="fixture-prior", sheet_row=2)
    specs = [prior, with_date(spec, value)]
    builder.write_json(tmp_path / "spec.json", {"schema": "g6-r1a-wirein-program-specs-v1", "programs": specs})
    builder.write_json(tmp_path / "authority.json", {"status": "APPROVED", "approved_scope": {
        "newly_wired_program_ids": [s["program_id"] for s in specs]}})
    builder.write_json(tmp_path / "sheet.json", {"status": "PASS", "selected_rows": [dict(
        row=s["sheet_row"], **{"Status": "approved_active_tracking_ready", "Signup Complete?": "TRUE",
        "Wire-In Ready?": "TRUE", "Network / Route": "Direct",
        "Tracking Link / Deep Link": "https://tracking.example/private"}) for s in specs]})
    argv = ["builder", "--source", str(source), "--target", str(output / "candidate"),
            "--sheet", str(tmp_path / "sheet.json"), "--authority", str(tmp_path / "authority.json"),
            "--spec", str(tmp_path / "spec.json"), "--research-dir", str(research_dir),
            "--report-dir", str(output / "reports")]
    monkeypatch.setattr(sys, "argv", argv)
    # Fixture-only external authorities; no real tracking validation or visits.
    for name, stub in dict(RESEARCH=research_dir, DECLARED_MUTATION_PATHS=set(),
        MATERIALIZATION_IGNORE_NAMES=(), _RailError=RuntimeError,
        _spec_allowed_host=lambda *args: None, _rail_for=lambda *args: None,
        _tracking_validation_id=lambda s: s["advertiser_id"],
        validate_tracking=lambda *args, **kwargs: ("tracking.example", {"affid": ["private-publisher"]}),
        tree_content_closure=lambda *args, **kwargs: {"tree_sha256": "fixture"}).items():
        monkeypatch.setitem(ns, name, stub)
    before = snapshot(tmp_path)
    directory_mtime = output.stat().st_mtime_ns
    with monkeypatch.context() as guarded:
        calls = trap_mutations(guarded, ns)
        expected = MutationReached if value == "2026-08-31" else RuntimeError
        with pytest.raises(expected) as error:
            builder.main()
        if expected is RuntimeError:
            assert "checked_as_of" in str(error.value) and "fixture-provider" in str(error.value)
            assert calls == []  # Includes cleanup; post-failure deletion is not accepted.
        else:
            assert calls == ["mutation"]  # Positive control reaches materialization.
    assert snapshot(tmp_path) == before
    assert output.stat().st_mtime_ns == directory_mtime
    assert sorted(p.name for p in output.iterdir()) == ["untouched.txt"]
