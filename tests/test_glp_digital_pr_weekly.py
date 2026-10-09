from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from scripts import glp_digital_pr_weekly as pr

NOW = "2026-08-08T12:00:00-07:00"


def record(provider: str, medication: str, amount: int | float, suffix: str = "base") -> dict:
    return {
        "id": f"{provider}-{medication}-{suffix}",
        "provider_id": provider,
        "medication": medication,
        "formulation": "published monthly program price",
        "billing_basis": "monthly",
        "price_phase": "starting",
        "selection_basis": "lowest_published_monthly_program_price",
        "amount_usd": amount,
    }


def observation(date: str, rows: list[dict]) -> dict:
    return {
        "observed_on": date,
        "data_as_of": date,
        "source_files": {
            "program_registry_sha256": "1" * 64,
            "program_content_ledger_sha256": "2" * 64,
            "original_data_study_sha256": "3" * 64,
        },
        "record_set_sha256": pr.sha256_bytes(pr.canonical_bytes(rows)),
        "records": rows,
    }


def tracker_for(current: list[dict], prior: list[dict] | None = None) -> dict:
    observations = []
    if prior is not None:
        observations.append(observation("2026-08-01", prior))
    observations.append(observation("2026-08-08", current))
    return {
        "schema": "fixture-price-tracker-v1",
        "price_index": {
            "schema": "glp-clinic-scout-price-index-v2",
            "route": pr.PRICE_INDEX_ROUTE,
            "publication_state": "PUBLIC_INDEXABLE",
            "records": current,
            "observations": observations,
        },
    }


def base_rows(sema: tuple[int, int] = (100, 100), tirz: tuple[int, int] = (200, 200)) -> list[dict]:
    return [
        record("provider-a", "semaglutide", sema[0]),
        record("provider-b", "semaglutide", sema[1]),
        record("provider-a", "tirzepatide", tirz[0]),
        record("provider-b", "tirzepatide", tirz[1]),
    ]


def target_row(index: int = 0, *, explicit: bool = True, evidence_date: str = "2026-08-01", inferred: bool = False) -> dict:
    row = {
        "name": f"Reporter {index:02d}",
        "outlet": f"Outlet {index:02d}",
        "beat": ["healthcare economics"],
        "article_url": f"https://example.com/article-{index}",
        "article_title": f"Article {index}",
        "evidence_date": evidence_date,
        "evidence": "The article directly covers GLP medication cost trends.",
        "contact_state": "verified_explicit" if explicit else "research_only",
    }
    if explicit or inferred:
        row["contact"] = {
            "address": f"reporter{index}@example.com",
            "evidence_url": f"https://example.com/author-{index}",
            "evidence_date": evidence_date,
            "evidence_sha256": f"{index % 10}" * 64,
            "method": "inferred_pattern" if inferred else "explicit_author_profile",
        }
    return row


def target_packet(rows: list[dict]) -> dict:
    return {
        "schema": "glp-clinic-scout-growth-build-2-research-v1",
        "window_days": 90,
        "targets": rows,
    }


def fixture_files(tmp_path: Path, tracker: dict, targets: dict) -> dict[str, Path]:
    releases = tmp_path / "site-system/flagship-v2/releases"
    release = releases / "fixture-release"
    predecessor = releases / "fixture-predecessor"
    predecessor.mkdir(parents=True)
    data = release / "data"
    data.mkdir(parents=True)
    (data / "price-tracker.json").write_text(json.dumps(tracker, indent=2) + "\n", encoding="utf-8")
    manifest = release / "manifest.json"
    final = release / "final-receipt.json"
    manifest.write_text('{"status":"PASS"}\n', encoding="utf-8")
    final.write_text('{"status":"PASS"}\n', encoding="utf-8")
    tree_hash, _ = pr.framed_tree_sha256(release)
    pointer = {
        "schema": "glp-opus-current-content-base-v1",
        "status": "PASS",
        "deployment_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "release_root": str(release),
        "release_tree_sha256": tree_hash,
        "public_tree_sha256": tree_hash,
        "manifest": {"path": str(manifest), "sha256": pr.sha256_bytes(manifest.read_bytes())},
        "final_receipt": {"path": str(final), "sha256": pr.sha256_bytes(final.read_bytes())},
        "supersedes": {
            "deployment_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "release_root": str(predecessor),
        },
    }
    pointer_path = tmp_path / "current-content-base.json"
    pointer_path.write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
    targets_path = tmp_path / "targets.json"
    targets_path.write_text(json.dumps(targets, indent=2) + "\n", encoding="utf-8")
    return {
        "releases": releases,
        "release": release,
        "pointer": pointer_path,
        "targets": targets_path,
        "db": tmp_path / "ops/control.sqlite3",
        "receipt": tmp_path / "reports/current.json",
        "send_receipts": tmp_path / "ops/send-receipts",
    }


def run_fixture(tmp_path: Path, tracker: dict, targets: dict) -> tuple[dict, dict[str, Path]]:
    paths = fixture_files(tmp_path, tracker, targets)
    result = pr.run_weekly(
        now=NOW,
        pointer_path=paths["pointer"],
        targets_path=paths["targets"],
        db_path=paths["db"],
        receipt_path=paths["receipt"],
        releases_root=paths["releases"],
    )
    return result, paths


@pytest.mark.parametrize(
    "supersedes",
    [
        "hostile-not-an-object",
        {"deployment_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
        {"release_root": "/tmp/hostile"},
        {
            "deployment_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "release_root": "/tmp/hostile",
            "unexpected": True,
        },
    ],
)
def test_digital_pr_consumer_rejects_hostile_supersedes_schema(
    tmp_path: Path, supersedes: object
) -> None:
    paths = fixture_files(tmp_path, tracker_for(base_rows()), target_packet([target_row()]))
    pointer = json.loads(paths["pointer"].read_text(encoding="utf-8"))
    pointer["supersedes"] = supersedes
    paths["pointer"].write_text(json.dumps(pointer) + "\n", encoding="utf-8")
    with pytest.raises(pr.DigitalPRError, match="supersedes_fields_invalid"):
        pr.resolve_content_base(paths["pointer"], releases_root=paths["releases"])


def test_framed_tree_hash_uses_canonical_relative_path_order(tmp_path: Path):
    release = tmp_path / "release"
    nested = release / "workers/price-watch/.wrangler/state.json"
    sibling = release / "workers/price-watch-scheduler.mjs"
    nested.parent.mkdir(parents=True)
    sibling.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(b"nested\n")
    sibling.write_bytes(b"sibling\n")

    rows = []
    for path in (nested, sibling):
        raw = path.read_bytes()
        rows.append((path.relative_to(release).as_posix(), pr.sha256_bytes(raw), len(raw)))
    digest = hashlib.sha256()
    for relative, file_hash, size in sorted(rows, key=lambda row: row[0]):
        digest.update(f"{relative}\0{file_hash}\0{size}\n".encode("utf-8"))

    assert pr.framed_tree_sha256(release) == (digest.hexdigest(), 2)


def test_cron_wrapper_treats_canonical_no_news_status_as_success_report():
    wrapper_path = Path("/home/ubuntu/.hermes/scripts/glp_digital_pr_weekly_cron.py")
    spec = importlib.util.spec_from_file_location("glp_digital_pr_weekly_cron_test", wrapper_path)
    assert spec is not None and spec.loader is not None
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)

    rendered = wrapper.render({
        "status": "NO_NEWS_NO_PITCH",
        "receipt_path": "/tmp/receipt.json",
        "pitch_drafts_created": 0,
        "outreach_sends": 0,
    })

    assert "weekly digital PR gate completed" in rendered
    assert "NO_NEWS_NO_PITCH" in rendered
    assert "needs review" not in rendered


def approval_messages(ledger: pr.DigitalPRLedger, pitch_id: str, *, actor: str = pr.OPERATOR_ID, content_suffix: str = ""):
    pitch = ledger.conn.execute("SELECT * FROM pitch_drafts WHERE pitch_id=?", (pitch_id,)).fetchone()
    card_content, binding = pr.render_approval_card(pitch)
    card_id = "card-100"
    decision_id = "decision-100"
    messages = {
        card_id: {
            "id": card_id,
            "channel_id": pr.DECISIONS_CHANNEL_ID,
            "content": card_content,
            "edited_timestamp": None,
            "author": {"id": "dispatcher", "bot": True},
        },
        decision_id: {
            "id": decision_id,
            "channel_id": pr.DECISIONS_CHANNEL_ID,
            "content": f"APPROVE GLP_PR_PITCH {pitch_id} {binding}{content_suffix}",
            "edited_timestamp": None,
            "author": {"id": actor, "bot": False},
            "message_reference": {"message_id": card_id},
        },
    }

    def fetch(channel_id: str, message_id: str) -> dict:
        assert channel_id == pr.DECISIONS_CHANNEL_ID
        return messages[message_id]

    return decision_id, fetch, messages


def make_news_run(tmp_path: Path, target_count: int = 1):
    current = base_rows(sema=(106, 106))
    prior = base_rows(sema=(100, 100))
    targets = target_packet([target_row(index) for index in range(target_count)])
    result, paths = run_fixture(tmp_path, tracker_for(current, prior), targets)
    assert result["status"] in {"NEWSWORTHY_DRAFTS_READY", "NEWSWORTHY_NO_ELIGIBLE_CONTACTS"}
    return result, paths


def test_no_news_refuses_pitch_and_is_same_day_idempotent_with_zero_effects(tmp_path: Path):
    result, paths = run_fixture(tmp_path, tracker_for(base_rows()), target_packet([target_row(0)]))
    assert result["status"] == "NO_NEWS_NO_PITCH"
    assert result["news_gate"]["matched_reasons"] == []
    assert result["targets"]["contact_evidence_hashes_available"] == 1
    assert result["targets"]["raw_capture_hashes_available"] == 0
    assert result["drafts"] == {"created": 0, "sent": 0}
    assert result["effects_this_run"]["external_sends"] == 0
    assert result["effects_this_run"]["provider_mutations"] == 0
    first_bytes = paths["receipt"].read_bytes()
    first_db_bytes = paths["db"].read_bytes()

    second = pr.run_weekly(
        now=NOW,
        pointer_path=paths["pointer"],
        targets_path=paths["targets"],
        db_path=paths["db"],
        receipt_path=paths["receipt"],
        releases_root=paths["releases"],
    )
    assert second["run_id"] == result["run_id"]
    assert paths["receipt"].read_bytes() == first_bytes
    assert paths["db"].read_bytes() == first_db_bytes
    with pr.DigitalPRLedger(paths["db"]) as ledger:
        assert ledger.conn.execute("SELECT COUNT(*) FROM gate_runs").fetchone()[0] == 1
        assert ledger.conn.execute("SELECT COUNT(*) FROM pitch_drafts").fetchone()[0] == 0
        assert ledger.conn.execute("SELECT COUNT(*) FROM send_reconciliations").fetchone()[0] == 0
        target_version = ledger.conn.execute("SELECT target_version_id FROM target_evidence").fetchone()[0]
        with pytest.raises(pr.DigitalPRError, match="pitch_refused_no_news"):
            pr.create_pitch_draft(ledger, result["run_id"], target_version)
        assert all(value == 0 for value in ledger.effects().values())


def test_each_mechanical_news_gate_and_variable_cardinality_new_provider():
    median_gate = pr.evaluate_newsworthiness(pr.normalize_price_observations(
        tracker_for(base_rows(sema=(106, 106)), base_rows(sema=(100, 100)))
    ))
    assert [row["gate"] for row in median_gate["matched_reasons"]] == ["absolute_median_change_at_least_5_percent"]

    spread_gate = pr.evaluate_newsworthiness(pr.normalize_price_observations(
        tracker_for(base_rows(sema=(100, 300)))
    ))
    assert [row["gate"] for row in spread_gate["matched_reasons"]] == ["spread_multiple_at_least_3x"]

    prior = base_rows(sema=(100, 100), tirz=(200, 200))
    current = base_rows(sema=(100, 100), tirz=(200, 200)) + [record("provider-c", "semaglutide", 90)]
    new_provider_gate = pr.evaluate_newsworthiness(pr.normalize_price_observations(tracker_for(current, prior)))
    assert len(current) == len(prior) + 1
    assert [row["gate"] for row in new_provider_gate["matched_reasons"]] == [
        "new_provider_below_current_medication_median"
    ]
    reason = new_provider_gate["matched_reasons"][0]
    assert reason["provider_id"] == "provider-c"
    assert reason["provider_absent_from_prior_snapshot"] is True
    assert reason["strictly_below_current_median"] is True


def test_target_expiry_and_inferred_contact_are_never_eligible():
    expired_document = target_packet([target_row(1, evidence_date="2026-05-08")])
    expired = pr.validate_target_packet(expired_document, "a" * 64, pr.parse_now(NOW))[0]
    assert expired["evidence_age_days"] == 92
    assert expired["eligible"] is False
    assert "target_article_evidence_expired" in expired["ineligible_reasons"]

    inferred_document = target_packet([target_row(2, explicit=False, inferred=True)])
    inferred = pr.validate_target_packet(inferred_document, "b" * 64, pr.parse_now(NOW))[0]
    assert inferred["contact_address"] is None
    assert inferred["eligible"] is False
    assert "contact_not_explicit" in inferred["ineligible_reasons"]


def test_two_explicit_contacts_and_research_only_sarah_are_counted_truthfully_with_no_news(tmp_path: Path):
    tanya = target_row(1)
    tanya.update({"name": "Tanya Akim", "outlet": "Reuters"})
    tanya["contact"]["method"] = "explicit_article_byline"
    anastassia = target_row(2)
    anastassia.update({"name": "Anastassia Gliadkovskaya", "outlet": "Fierce Healthcare"})
    anastassia["contact"]["method"] = "explicit_author_profile"
    sarah = target_row(3, explicit=False)
    sarah.update({"name": "Sarah Davis", "outlet": "Philadelphia Business Journal"})

    result, paths = run_fixture(
        tmp_path,
        tracker_for(base_rows()),
        target_packet([tanya, anastassia, sarah]),
    )

    assert result["status"] == "NO_NEWS_NO_PITCH"
    assert result["targets"]["researched"] == 3
    assert result["targets"]["addresses_eligible"] == 2
    assert result["targets"]["contact_evidence_hashes_available"] == 2
    assert result["targets"]["raw_capture_hashes_available"] == 0
    assert len(result["targets"]["eligible_target_ids"]) == 2
    assert result["drafts"] == {"created": 0, "sent": 0}
    assert result["send_receipts_created"] == 0
    assert all(value == 0 for value in result["effects_this_run"].values())
    with pr.DigitalPRLedger(paths["db"]) as ledger:
        states = {
            row["name"]: bool(row["eligible"])
            for row in ledger.conn.execute("SELECT name,eligible FROM target_evidence")
        }
        assert states == {
            "Anastassia Gliadkovskaya": True,
            "Sarah Davis": False,
            "Tanya Akim": True,
        }
        assert ledger.conn.execute("SELECT COUNT(*) FROM pitch_drafts").fetchone()[0] == 0


def test_canonical_packet_matches_only_the_two_explicit_firecrawl_contacts():
    evidence_path = (
        pr.BUSINESS
        / "reports/glp-clinic-scout/growth/build-2/firecrawl-contact-evidence-20260808.json"
    )
    target_document, _target_raw = pr.read_json(pr.TARGETS_PATH)
    evidence_document, _evidence_raw = pr.read_json(evidence_path)
    target_by_name = {row["name"]: row for row in target_document["targets"]}
    evidence_by_name = {row["name"]: row for row in evidence_document["targets"]}

    assert set(target_by_name) == {
        "Anastassia Gliadkovskaya",
        "Sarah Davis",
        "Tanya Akim",
    }
    expected_methods = {
        "Tanya Akim": "explicit_article_byline",
        "Anastassia Gliadkovskaya": "explicit_author_profile",
    }
    for name, method in expected_methods.items():
        source = evidence_by_name[name]
        assert target_by_name[name]["contact_state"] == "verified_explicit"
        assert target_by_name[name]["contact"] == {
            "address": source["contact_address"],
            "evidence_url": source["source_url"],
            "evidence_date": evidence_document["captured_on"],
            "evidence_sha256": source["evidence_sha256"],
            "method": method,
        }
    assert target_by_name["Sarah Davis"]["contact_state"] == "research_only"
    assert "contact" not in target_by_name["Sarah Davis"]


def test_news_filters_eligible_targets_before_applying_authoritative_cap_10(tmp_path: Path):
    rows = [target_row(0, explicit=False), *[target_row(index) for index in range(1, 11)]]
    result, _paths = run_fixture(
        tmp_path,
        tracker_for(base_rows(sema=(106, 106)), base_rows(sema=(100, 100))),
        target_packet(rows),
    )
    assert pr.WEEKLY_SEND_CAP == 10
    assert result["week"]["send_cap"] == 10
    assert result["targets"]["researched"] == 11
    assert result["targets"]["addresses_eligible"] == 10
    assert result["drafts"] == {"created": 10, "sent": 0}


def test_monday_sunday_quota_counts_verified_plus_unresolved_permits_10_rejects_11_and_not_drafts(tmp_path: Path):
    assert pr.WEEKLY_SEND_CAP == 10
    result, paths = make_news_run(tmp_path, target_count=11)
    with pr.DigitalPRLedger(paths["db"]) as ledger:
        target_versions = [row[0] for row in ledger.conn.execute("SELECT target_version_id FROM target_evidence ORDER BY target_version_id")]
        for target_version in target_versions:
            pr.create_pitch_draft(ledger, result["run_id"], target_version)
        pitches = [row[0] for row in ledger.conn.execute("SELECT pitch_id FROM pitch_drafts ORDER BY pitch_id")]
        assert len(pitches) == 11
        assert ledger.conn.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 0
        reservations = [pr.reserve_weekly_quota(ledger, pitch_id, now=NOW) for pitch_id in pitches[:10]]
        assert all(row["week_start"] == "2026-08-03" and row["week_end"] == "2026-08-09" for row in reservations)
        replay = pr.reserve_weekly_quota(ledger, pitches[0], now=NOW)
        assert replay["idempotent_replay"] is True
        assert ledger.conn.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 10
        with pytest.raises(pr.WeeklyQuotaExceeded, match="weekly_quota_exceeded"):
            pr.reserve_weekly_quota(ledger, pitches[10], now=NOW)
        ledger.conn.execute("UPDATE quota_reservations SET status='sent_verified' WHERE pitch_id=?", (pitches[0],))
        ledger.conn.commit()
        with pytest.raises(pr.WeeklyQuotaExceeded, match="weekly_quota_exceeded"):
            pr.reserve_weekly_quota(ledger, pitches[10], now=NOW)


def test_operator_approval_is_exact_actor_channel_card_and_immutable_hash_bound(tmp_path: Path):
    _result, paths = make_news_run(tmp_path)
    with pr.DigitalPRLedger(paths["db"]) as ledger:
        pitch_id = ledger.conn.execute("SELECT pitch_id FROM pitch_drafts").fetchone()[0]
        decision_id, bad_actor_fetch, _ = approval_messages(ledger, pitch_id, actor="someone-else")
        with pytest.raises(pr.DigitalPRError, match="approval_actor_mismatch"):
            pr.validate_operator_approval(ledger, pitch_id, decision_id, fetch_message=bad_actor_fetch, now=NOW)

        decision_id, bad_content_fetch, _ = approval_messages(ledger, pitch_id, content_suffix=" changed")
        with pytest.raises(pr.DigitalPRError, match="approval_decision_payload_mismatch"):
            pr.validate_operator_approval(ledger, pitch_id, decision_id, fetch_message=bad_content_fetch, now=NOW)

        decision_id, fetch, messages = approval_messages(ledger, pitch_id)
        approved = pr.validate_operator_approval(ledger, pitch_id, decision_id, fetch_message=fetch, now=NOW)
        assert approved["actor_id"] == pr.OPERATOR_ID
        assert approved["channel_id"] == pr.DECISIONS_CHANNEL_ID
        pitch = ledger.conn.execute("SELECT * FROM pitch_drafts WHERE pitch_id=?", (pitch_id,)).fetchone()
        binding = pr.approval_binding(pitch)
        assert approved["binding_sha256"] == pr.sha256_bytes(pr.canonical_bytes(binding))
        assert {
            "recipient_sha256", "subject_sha256", "body_sha256", "observation_sha256", "pitch_id"
        } <= set(binding)
        messages["card-100"]["edited_timestamp"] = "2026-08-08T20:01:00Z"
        with pytest.raises(pr.DigitalPRError, match="approval_card_not_exact_active"):
            pr.validate_operator_approval(ledger, pitch_id, decision_id, fetch_message=fetch, now=NOW)


def test_crash_after_submission_recovers_by_identity_without_duplicate_and_reconciles_once(tmp_path: Path):
    _result, paths = make_news_run(tmp_path)
    with pr.DigitalPRLedger(paths["db"]) as ledger:
        pitch_id = ledger.conn.execute("SELECT pitch_id FROM pitch_drafts").fetchone()[0]
        pitch = ledger.conn.execute("SELECT * FROM pitch_drafts WHERE pitch_id=?", (pitch_id,)).fetchone()
        decision_id, fetch, _messages = approval_messages(ledger, pitch_id)
        calls: list[bool] = []

        class SubmissionUncertain(RuntimeError):
            submission_started = True

        def first_transport(_binding, *, allow_new_submission, on_submission_start):
            calls.append(allow_new_submission)
            assert allow_new_submission is True
            on_submission_start()
            raise SubmissionUncertain("provider timeout")

        with pytest.raises(SubmissionUncertain):
            pr.send_approved_pitch(
                ledger, pitch_id, decision_id, fetch_message=fetch, transport=first_transport,
                mailbox={"mailbox_id": pr.MAILBOX_ID}, now="2026-08-09T23:59:00-07:00",
                receipt_root=paths["send_receipts"],
            )
        assert ledger.conn.execute("SELECT status FROM quota_reservations WHERE pitch_id=?", (pitch_id,)).fetchone()[0] == "reconcile_required"

        def recovery_transport(binding, *, allow_new_submission, on_submission_start):
            calls.append(allow_new_submission)
            assert allow_new_submission is False
            return {
                "status": "ALREADY_SENT_VERIFIED",
                "send_executed_now": False,
                "provider_message_id": "gmail-message-1",
                "provider_thread_id": "gmail-thread-1",
                "provider_internal_date": "1786237200000",
                "checks": {"all_passed": True},
                "readback": {
                    "message_id": "<glp-pr-fixture@glpclinicscout.com>",
                    "from": pr.SENDER,
                    "to": pitch["recipient"],
                    "reply_to": pr.SENDER,
                    "body": pitch["body"],
                    "body_sha256": pitch["body_sha256"],
                    "identity_headers": {"X-Hermes-GLP-PR-Pitch-ID": pitch_id},
                },
            }

        sent = pr.send_approved_pitch(
            ledger, pitch_id, decision_id, fetch_message=fetch, transport=recovery_transport,
            mailbox={"mailbox_id": pr.MAILBOX_ID}, now="2026-08-10T00:01:00-07:00",
            receipt_root=paths["send_receipts"],
        )
        assert sent["status"] == "SENT_VERIFIED"
        assert sent["send_executed_now"] is False
        assert calls == [True, False]
        assert sent["week_start"] == "2026-08-03"
        assert sent["week_end"] == "2026-08-09"
        assert ledger.conn.execute("SELECT COUNT(*) FROM send_reconciliations").fetchone()[0] == 1
        assert ledger.effects()["external_sends"] == 1
        assert ledger.effects()["provider_mutations"] == 1

        def forbidden_transport(*_args, **_kwargs):
            raise AssertionError("transport must not be called for a locally reconciled pitch")

        replay = pr.send_approved_pitch(
            ledger, pitch_id, decision_id, fetch_message=fetch, transport=forbidden_transport,
            mailbox={"mailbox_id": pr.MAILBOX_ID}, now="2026-08-10T00:02:00-07:00",
            receipt_root=paths["send_receipts"],
        )
        assert replay["idempotent_replay"] is True
        assert ledger.conn.execute("SELECT COUNT(*) FROM send_reconciliations").fetchone()[0] == 1
        assert ledger.effects()["external_sends"] == 1


def test_sender_and_zero_effect_held_state_are_explicit(tmp_path: Path):
    result, paths = run_fixture(tmp_path, tracker_for(base_rows()), target_packet([target_row(0, explicit=False)]))
    receipt = json.loads(paths["receipt"].read_text(encoding="utf-8"))
    assert result["sender_if_future_approved"] == "izaiah@glpclinicscout.com"
    assert receipt["targets"]["addresses_eligible"] == 0
    assert receipt["drafts"]["sent"] == 0
    assert receipt["send_receipts_created"] == 0
    assert receipt["send_receipt_fabricated"] is False
    assert receipt["effects_this_run"] == {
        "external_sends": 0,
        "provider_mutations": 0,
        "discord_posts": 0,
        "deployments": 0,
        "release_pointer_mutations": 0,
        "cron_registrations": 0,
        "spend_cents": 0,
        "secret_reads": 0,
    }
    assert not paths["send_receipts"].exists()
