from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import fleet_pin_release as release  # noqa: E402
import pin_drift_report as drift  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review(path: Path, entries: list[Path]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "fleet-pin-review-v1",
                "decision": "ACCEPT_EXACT_CURRENT_BYTES",
                "reviewer": "fixture-reviewer",
                "reviewed_at_utc": "2026-08-25T00:00:00Z",
                "entries": [
                    {
                        "path": str(entry),
                        "reviewed_sha256": sha(entry),
                        "review_basis": "full_current_baseline_review",
                        "evidence": ["fixture review and deterministic tests"],
                    }
                    for entry in entries
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def pin_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    business = tmp_path / "business"
    scripts = business / "scripts"
    hermes_scripts = tmp_path / "hermes-scripts"
    archive = business / "reports/ops/pin-archive/sha256"
    scripts.mkdir(parents=True)
    hermes_scripts.mkdir()
    payload = scripts / "payload.py"
    carrier = hermes_scripts / "carrier.py"
    wrapper = hermes_scripts / "wrapper.sh"
    payload.write_text("VALUE = 'reviewed current bytes'\n", encoding="utf-8")
    stale = "0" * 64
    carrier.write_text(
        "from pathlib import Path\n"
        f"PAYLOAD = Path({str(payload)!r})\n"
        f'EXPECTED_PAYLOAD_SHA256 = "{stale}"\n',
        encoding="utf-8",
    )
    wrapper.write_text(
        f"readonly CARRIER={carrier}\n"
        f"readonly CARRIER_SHA256={stale}\n",
        encoding="utf-8",
    )
    carrier.chmod(0o700)
    wrapper.chmod(0o700)
    registry = tmp_path / "jobs.json"
    registry.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "carrier-job",
                        "enabled": True,
                        "script": wrapper.name,
                        "script_sha256": stale,
                    },
                    {
                        "id": "unpinned-job",
                        "enabled": True,
                        "script": wrapper.name,
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(drift, "BUSINESS", business)
    monkeypatch.setattr(drift, "HERMES_SCRIPTS", hermes_scripts)
    monkeypatch.setattr(drift, "PIN_ARCHIVE", archive)
    monkeypatch.setattr(drift, "PYTHON_PIN_ROOTS", ())
    monkeypatch.setattr(drift, "PYTHON_SCALAR_PIN_ROOTS", (hermes_scripts,))
    monkeypatch.setattr(drift, "PYTHON_PIN_SOURCES", ())
    monkeypatch.setattr(drift, "HISTORICAL_RECORD_SOURCES", frozenset())
    monkeypatch.setattr(release, "ARCHIVE_ROOT", archive.parent)
    monkeypatch.setattr(release, "SECRET_ROOT", tmp_path / "secrets")
    monkeypatch.setattr(release, "LOCK", tmp_path / "release.lock")
    return {
        "payload": payload,
        "carrier": carrier,
        "wrapper": wrapper,
        "registry": registry,
    }


def test_reviewed_release_walks_scalar_shell_registry_and_archives(pin_graph, tmp_path):
    review = tmp_path / "review.json"
    reviewed = [pin_graph[key] for key in ("payload", "carrier", "wrapper")]
    _review(review, reviewed)
    registry = release.JsonRegistry(pin_graph["registry"])

    code, receipt = release.release(
        review,
        registry,
        registry_path=pin_graph["registry"],
        apply=True,
    )

    assert code == 0
    assert receipt["status"] == "PASS"
    assert receipt["applied"] is True
    assert drift.scan(
        include_shell=True,
        include_registry=True,
        registry=pin_graph["registry"],
        scope="fleet",
    ) == []
    assert sha(pin_graph["payload"]) in pin_graph["carrier"].read_text(encoding="utf-8")
    assert sha(pin_graph["carrier"]) in pin_graph["wrapper"].read_text(encoding="utf-8")
    jobs = {job["id"]: job for job in registry.jobs()}
    assert jobs["carrier-job"]["script_sha256"] == sha(pin_graph["wrapper"])
    assert "script_sha256" not in jobs["unpinned-job"]
    assert {row["job_id"] for row in receipt["registry_updates"]} == {"carrier-job"}
    for path in reviewed:
        accepted = drift.archive_path(sha(path))
        assert accepted.read_bytes() == path.read_bytes()


def test_unreviewed_live_drift_blocks_without_mutation(pin_graph, tmp_path):
    review = tmp_path / "review.json"
    _review(review, [pin_graph["carrier"], pin_graph["wrapper"]])
    before = {key: path.read_bytes() for key, path in pin_graph.items()}
    registry = release.JsonRegistry(pin_graph["registry"])

    code, receipt = release.release(
        review,
        registry,
        registry_path=pin_graph["registry"],
        apply=True,
    )

    assert code == 2
    assert receipt["status"] == "BLOCKED"
    assert any(
        item["path"] == str(pin_graph["payload"])
        for item in receipt["unreviewed_findings"]
    )
    assert {key: path.read_bytes() for key, path in pin_graph.items()} == before


def test_future_review_cannot_authorize_bytes(pin_graph, tmp_path):
    review = tmp_path / "review.json"
    _review(review, [pin_graph["payload"]])
    document = json.loads(review.read_text(encoding="utf-8"))
    document["reviewed_at_utc"] = "2999-01-01T00:00:00Z"
    review.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeError, match="review_timestamp_in_future"):
        release.release(
            review,
            release.JsonRegistry(pin_graph["registry"]),
            registry_path=pin_graph["registry"],
            apply=False,
        )


def test_review_cannot_accept_or_archive_secret_bytes(tmp_path, monkeypatch):
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    secret = secret_root / "api.json"
    secret.write_text('{"api_key":"do-not-print"}\n', encoding="utf-8")
    review = tmp_path / "review.json"
    _review(review, [secret])
    monkeypatch.setattr(release, "SECRET_ROOT", secret_root)

    with pytest.raises(RuntimeError, match="review_entry_contract_invalid"):
        release.load_review(review)
