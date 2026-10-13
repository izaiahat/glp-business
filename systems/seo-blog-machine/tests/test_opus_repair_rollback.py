"""Source-only packet fixtures: no reviewer, validator or publisher invocation."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opus_review_loop as review  # noqa: E402


def test_reviewer_title_repairs_must_preserve_deterministic_bounds(tmp_path, monkeypatch):
    rubric = tmp_path / "rubric.md"
    rubric.write_text("All five clinical gates A-E remain mandatory.")
    monkeypatch.setattr(review, 'RUBRIC', rubric)
    monkeypatch.setattr(review.review_transports, 'transport', lambda _: {})
    prompt = review.build_prompt({}, {'manifest_sha256':'a' * 64}, tmp_path / 'manifest.json', 1, tmp_path).read_text()
    assert '50–60 characters' in prompt
    assert 'Never prescribe an exact out-of-range' in prompt
    assert 'original clinical and claim findings remain blocking' in prompt
    assert 'Regenerate derived article and QA' in prompt


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("final_newline", [False, True])
def test_context_bundle_keeps_long_json_quote_on_its_physical_line(tmp_path, separator, final_newline):
    packet = tmp_path / "packet"
    packet.mkdir()
    quote = "a" * 10200 + separator + "b" * 3300
    text = json.dumps({"quote_id": "S5Q1", "text": quote}, ensure_ascii=False) + ("\n" if final_newline else "")
    subjects = ["01-ledger.json", "02.json", "03.json", "04.json", "05.json"]
    for name in subjects:
        (packet / name).write_text(text, encoding="utf-8")
    history = tmp_path / "history"
    history.mkdir()
    job = {"packet_dir": str(packet), "subject_files": subjects, "job_id": "bundle-fixture", "topic_id": "fixture"}
    manifest, _ = review.seal_subjects(job, 1, history)
    bundle = review.build_context_bundle(job, manifest, 1, history).read_text(encoding="utf-8")
    records = [line for line in bundle.split("\n") if line.startswith("1|")]
    assert len(records) == len(subjects)
    assert all(json.loads(line.partition("|")[2])["text"] == quote for line in records)
    assert all(row["lines"] == 1 for row in manifest["subject_files"])
    assert "\n2|" not in bundle
    assert all((packet / name).read_text(encoding="utf-8") == text for name in subjects)


@pytest.mark.parametrize("raw, expected", [
    (b'', []),
    (b'\n', ['']),
    (b'{\r"a":1}\n', ['{\r"a":1}']),
    (b'first\rsecond\nthird\r\nfourth\r', ['first\rsecond', 'third', 'fourth\r']),
    (b'first\nsecond', ['first', 'second']),
    (b'first\nsecond\n', ['first', 'second']),
    (b'first\r\nsecond\r\n', ['first', 'second']),
    (b'\r', ['\r']),
])
def test_physical_line_split_preserves_raw_non_delimiter_cr(raw, expected):
    assert review.physical_source_lines(raw) == expected


@pytest.fixture
def repair_case(tmp_path):
    packet = tmp_path / "packet"
    packet.mkdir()
    originals = {
        "01-brief.json": b'{"topic_id":"fixture"}\n',
        "02-sources.json": b'{"sources":[]}\n',
        "03-outline.md": b"# Outline\n",
        "04-draft.md": b"# Original draft\n",
        "05-claim-source-map.json": b'{"claims":[]}\n',
    }
    for rel, content in originals.items():
        (packet / rel).write_bytes(content)
    executable = Path(sys.executable).resolve()
    spec = {
        "executable": str(executable),
        "executable_sha256": review.sha_file(executable),
        "argv": [str(executable), "-c", "raise AssertionError('unexpected child')"],
        "timeout": 1,
        "derived_files": ["05-claim-source-map.json"],
    }
    job = {
        "job_id": "isolated-rollback-fixture",
        "packet_dir": str(packet),
        "subject_files": list(originals),
        "repair": spec,
        "validator": dict(spec),
    }
    payload = {"findings": [{"id": "A-01", "file": "04-draft.md"}]}
    history = tmp_path / "history"
    before = review.subject_snapshot(packet, review.subject_paths(job))
    return packet, originals, job, payload, history, before


def assert_restored(packet, originals):
    assert {rel: (packet / rel).read_bytes() for rel in originals} == originals
    assert all((packet / rel).stat().st_mode & 0o777 == 0o600 for rel in originals)


def assert_no_replay(monkeypatch, job, payload, history, before):
    forbidden = Mock(side_effect=AssertionError("repair must not replay"))
    monkeypatch.setattr(review.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="receipt_exists:.*repair-plan.json"):
        review.run_repair(job, payload, 1, history, before)
    forbidden.assert_not_called()


def run_isolated_child(monkeypatch, repair_case, code):
    packet, originals, job, payload, history, before = repair_case
    # Only the repair subprocess runs; this script is confined to fixture cwd.
    child = packet.parent / "repair-fixture.py"
    child.write_text(code, encoding="utf-8")
    job["repair"]["argv"] = [job["repair"]["executable"], str(child)]
    job["repair"]["control_files"] = {str(child): review.sha_file(child)}
    calls = Mock(wraps=subprocess.run)
    monkeypatch.setattr(review.subprocess, "run", calls)
    return calls


def test_timeout_after_mutation_restores_original_subject_bytes(monkeypatch, repair_case):
    packet, originals, job, payload, history, before = repair_case
    calls = run_isolated_child(monkeypatch, repair_case,
        "from pathlib import Path\nimport time\n"
        "Path('04-draft.md').write_bytes(b'mutated before timeout')\n"
        "time.sleep(30)\n")
    with pytest.raises(subprocess.TimeoutExpired) as error:
        review.run_repair(job, payload, 1, history, before)
    assert error.value.cmd == job["repair"]["argv"]
    calls.assert_called_once()
    assert_restored(packet, originals)
    assert_no_replay(monkeypatch, job, payload, history, before)


def test_deleted_subject_after_mutation_restores_saved_paths(monkeypatch, repair_case):
    packet, originals, job, payload, history, before = repair_case
    calls = run_isolated_child(monkeypatch, repair_case,
        "from pathlib import Path\n"
        "Path('04-draft.md').write_bytes(b'mutated before deletion')\n"
        "Path('05-claim-source-map.json').unlink()\n")
    with pytest.raises(RuntimeError, match="subject_missing_or_unsafe:05-claim-source-map.json"):
        review.run_repair(job, payload, 1, history, before)
    calls.assert_called_once()
    assert_restored(packet, originals)
    assert_no_replay(monkeypatch, job, payload, history, before)


@pytest.mark.parametrize("scenario,expected", [
    ("nonzero", "repair_command_failed"),
    ("no_change", "repair_no_changes"),
    ("scope", "repair_scope_violation"),
    ("derived_only", "repair_scope_violation"),
    ("allowed", None),
    ("allowed_and_derived", None),
])
def test_existing_repair_scope_and_success_contract(monkeypatch, repair_case, scenario, expected):
    packet, originals, job, payload, history, before = repair_case

    def child(*args, **kwargs):
        if scenario in {"nonzero", "scope", "allowed", "allowed_and_derived"}:
            (packet / "04-draft.md").write_bytes(b"allowed change")
        if scenario == "scope":
            (packet / "03-outline.md").write_bytes(b"unauthorized change")
        if scenario in {"derived_only", "allowed_and_derived"}:
            (packet / "05-claim-source-map.json").write_bytes(b"derived change")
        return subprocess.CompletedProcess(args[0], int(scenario == "nonzero"), "", "")

    calls = Mock(side_effect=child)
    monkeypatch.setattr(review.subprocess, "run", calls)
    if expected:
        with pytest.raises(RuntimeError, match=f"^{expected}$"):
            review.run_repair(job, payload, 1, history, before)
        assert_restored(packet, originals)
    else:
        review.run_repair(job, payload, 1, history, before)
        assert (packet / "04-draft.md").read_bytes() == b"allowed change"
        if scenario == "allowed_and_derived":
            assert (packet / "05-claim-source-map.json").read_bytes() == b"derived change"
    calls.assert_called_once()
    receipt = json.loads((history / "round-01-repair-execution.json").read_text())
    assert receipt["exit_code"] == int(scenario == "nonzero")
    assert_no_replay(monkeypatch, job, payload, history, before)


@pytest.mark.parametrize("seam", ["execution_receipt", "snapshot"])
def test_post_repair_exception_restores_bytes_and_reraises(monkeypatch, repair_case, seam):
    packet, originals, job, payload, history, before = repair_case
    original_error = OSError("injected post-repair failure")

    def child(*args, **kwargs):
        (packet / "04-draft.md").write_bytes(b"mutated")
        return subprocess.CompletedProcess(args[0], 0, "", "")

    calls = Mock(side_effect=child)
    monkeypatch.setattr(review.subprocess, "run", calls)
    if seam == "snapshot":
        monkeypatch.setattr(review, "subject_snapshot", Mock(side_effect=original_error))
    else:
        original_atomic = review.atomic_json

        def atomic(path, value, **kwargs):
            if path.name.endswith("repair-execution.json"):
                raise original_error
            return original_atomic(path, value, **kwargs)

        monkeypatch.setattr(review, "atomic_json", atomic)
    with pytest.raises(OSError) as error:
        review.run_repair(job, payload, 1, history, before)
    assert error.value is original_error
    calls.assert_called_once()
    assert_restored(packet, originals)
    assert_no_replay(monkeypatch, job, payload, history, before)
