"""Exact ID37 manual handoff. A live operator reply is intent, not acceptance.

No daemon dispatch, caller-selected reviewer, deploy approval, or paid fallback.
The parent adopts this source only after independent review and exact-head CI.
"""
from contextlib import contextmanager
from typing import Any
import hashlib
import re
import sys
import time
from pathlib import Path

REQUEST_ID = "1555525422467387402"
ISSUE_ID = "bridge-issue-7444094b1806654ef6b225f19e8f3f58"
TUPLE_SHA256 = "05b7986fdc21c53e188d53ecc7ed7a25e7c846921cfd62b8bc372c469cd6fdc9"
SUBJECT_ID = "glp-clinic-scout:F06:lp-quiz-issued-version:20261002"
DEFERRED_ID = "1555525443363278849"
DECISIONS_CHANNEL = "1524254375453786294"
QUESTION_ID = "1555539647889805455"
QUESTION_SHA256 = "1aee963a9a56ba224130672277256c80ba1defb637344d7c744d5375ac81ca9e"
LEGACY_SESSION_ID = "171df31c-619c-4432-8a32-1767a9c8edc2"
NATIVE_SESSION_ID = "20260922_214459_3be4d1"
BACKEND = ("fable", "claude-fable-5-1", "xhigh")


def pin_native_profile():
    """Manual child only: fix home AND explicit profile before any Hermes import."""
    import os
    args = sys.argv[2:]
    required = {"--provider": "claude-subscription-directsdk-experimental",
                "-m": BACKEND[1], "--reasoning": BACKEND[2],
                "--resume": NATIVE_SESSION_ID, "--toolsets": "fable-review-readonly"}
    if (args[:1] != ["chat"] or "--oneshot" not in args
            or any(flag in args for flag in ("--profile", "-p", "--continue", "-c"))
            or any(args.count(flag) != 1 or args.index(flag) + 1 >= len(args)
                   or args[args.index(flag) + 1] != value for flag, value in required.items())):
        raise RuntimeError("parent_handoff_native_route_changed")
    os.environ["HERMES_HOME"] = str(Path.home() / ".hermes")
    # main.py's import-time profile pre-parser must not follow active_profile.
    sys.argv = ["hermes", "--profile", "default", *args]


@contextmanager
def native_resume_guard(expected_db, transcript):
    """Adapt the traced installed oneshot resume boundary, in this child only.

    _run_agent calls _load_resume_target with its acquired SessionDB and uses the
    returned final ID/meta before provider resolution or AIAgent construction.
    Delegate resolution/safe-resume/history to that real API; never reconstruct it.
    This is an explicit process-local adapter, not a saved profile or fork edit.
    """
    import inspect
    import json
    from hermes_cli import oneshot
    original = getattr(oneshot, "_load_resume_target", None)
    if not callable(original):
        raise RuntimeError("parent_handoff_native_resume_api_unavailable")
    try:
        parameters = tuple(inspect.signature(original).parameters)
    except (TypeError, ValueError):
        parameters = ()
    if parameters != ("session_db", "resume"):
        raise RuntimeError("parent_handoff_native_resume_api_unavailable")

    def bound_resume(session_db, resume):
        try:
            if (session_db is None or resume != NATIVE_SESSION_ID
                    or Path(session_db.db_path).resolve() != Path(expected_db).resolve()):
                raise RuntimeError("parent_handoff_native_store_mismatch")
            # Read the actual SQLite connection used by SessionDB reads, not an
            # environment value or the caller's claimed requested session.
            with session_db._read_ctx() as db:
                opened = [row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"]
            if len(opened) != 1 or Path(opened[0]).resolve() != Path(expected_db).resolve():
                raise RuntimeError("parent_handoff_native_store_mismatch")
            resolved, history, meta = original(session_db, resume)
            if (resolved != NATIVE_SESSION_ID or not isinstance(meta, dict)
                    or meta.get("id") != NATIVE_SESSION_ID):
                raise RuntimeError("parent_handoff_native_session_redirected")
            origin = json.loads(meta.get("origin_json") or "{}")
            imported = origin.get("imported_from", {}) if isinstance(origin, dict) else {}
            current = session_db.get_session(resolved) or {}
            if (transcript is None or not Path(transcript).is_file()
                    or not isinstance(imported, dict) or meta.get("source") != "claude-code"
                    or imported.get("tool") != "claude-code"
                    or imported.get("foreign_session_id") != LEGACY_SESSION_ID
                    or Path(imported.get("path", "")).resolve() != Path(transcript).resolve()
                    or any(current.get(key) != meta.get(key) for key in ("id", "source", "origin_json"))):
                raise RuntimeError("parent_handoff_native_import_mismatch")
            return resolved, history, meta
        except BaseException:
            # Native _run_agent's cleanup try starts after this boundary.
            if session_db is not None:
                session_db.close()
            raise

    oneshot._load_resume_target = bound_resume
    try:
        yield
    finally:
        oneshot._load_resume_target = original


@contextmanager
def _host_admission():
    # Installed API, shared with native children and CLIs. No new store/queue.
    runtime = str(Path.home() / ".hermes/hermes-agent")
    if runtime not in sys.path:
        sys.path.insert(0, runtime)
    from tools import delegation_admission as admission
    if admission.admission_problem() is not None:
        raise RuntimeError("parent_handoff_host_pressure")
    leases, _, _ = admission.try_reserve_host_children(1)
    if not leases:
        raise RuntimeError("parent_handoff_host_capacity")
    try:
        yield
    finally:
        for lease in leases:
            lease.release()  # subprocess.run has completed/terminated before release.


def _operator_intent(guard, bridge, message_id):
    question = guard._fetch_discord_message(DECISIONS_CHANNEL, QUESTION_ID)
    question_content = guard._message_identity(question, channel_id=DECISIONS_CHANNEL,
        message_id=QUESTION_ID, author_id=bridge.KAEL_BOT_ID, role="handoff_question")
    guard._reject_edited_message(question, "handoff_question")
    if (guard.sha256_text(question_content) != QUESTION_SHA256
            or question.get("author", {}).get("bot") is not True
            or question.get("webhook_id") is not None or question.get("type") != 0
            or "edited_timestamp" not in question):
        raise RuntimeError("parent_handoff_question_changed")
    message = guard._fetch_discord_message(DECISIONS_CHANNEL, message_id)
    content = guard._message_identity(message, channel_id=DECISIONS_CHANNEL,
        message_id=message_id, author_id=bridge.OPERATOR_ID, role="handoff_operator")
    guard._reject_edited_message(message, "handoff_operator")
    reference = message.get("message_reference") or {}
    expected = f"HANDOFF FABLE REVIEW ONLY {REQUEST_ID} {ISSUE_ID} {TUPLE_SHA256}"
    if (content != expected or message.get("author", {}).get("bot", False) is not False
            or message.get("webhook_id") is not None or message.get("type") != 19
            or "edited_timestamp" not in message or message.get("attachments") != []
            or reference.get("message_id") != QUESTION_ID
            or reference.get("channel_id") != DECISIONS_CHANNEL
            or int(message_id) <= int(QUESTION_ID)
            or guard._timestamp(message.get("timestamp"), "handoff_operator") <=
               guard._timestamp(question.get("timestamp"), "handoff_question")):
        raise RuntimeError("parent_handoff_intent_not_exact_review_only_reply")
    return {"operator_intent_message_id": message_id, "operator_id": bridge.OPERATOR_ID,
            "operator_intent_sha256": guard.sha256_text(content),
            "operator_intent_timestamp": message["timestamp"], "question_message_id": QUESTION_ID}


def dispatch(bridge, source_id, issue_id, tuple_sha256, operator_intent_message_id):
    """Maintained parent entry; CLI arguments locate evidence, never grant authority."""
    if ((source_id, issue_id, tuple_sha256) != (REQUEST_ID, ISSUE_ID, TUPLE_SHA256)
            or not re.fullmatch(r"[0-9]{17,20}", operator_intent_message_id)):
        raise RuntimeError("parent_handoff_exact_scope_required")
    if bridge.SELF_ID != bridge.expected_author_id() or bridge.SELF_ID == bridge.KAEL_BOT_ID:
        raise RuntimeError("independent_signer_mismatch")
    guard = bridge._authority_guard()
    path = bridge.ISSUE_DIR / f"{issue_id}.json"
    with bridge.bridge_state_lock():
        # Missing/corrupt state does not prove absence of an uncertain inference.
        import json
        state = json.loads(bridge.STATE.read_text(encoding="utf-8"))
        receipts = state.get("reviewer_receipts")
        receipt = receipts.get(source_id) if isinstance(receipts, dict) else None
        if (not isinstance(receipt, dict) or receipt.get("source_message_id") != source_id
                or receipt.get("kind") != "review_request" or receipt.get("mode") != "deferred"
                or receipt.get("attempts") != [] or receipt.get("posting") != "posted"
                or receipt.get("reviewer_identity") != "none (top_stakes_deferred)"
                or receipt.get("posted_message_id") != DEFERRED_ID
                or any(key in receipt for key in ("parent_handoff", "resume", "recovery", "invocation_claimed"))
                or not isinstance(state.get("last_id"), str) or not state["last_id"].isdigit()
                or int(state["last_id"]) < int(DEFERRED_ID)
                or any(key != source_id and isinstance(value, dict)
                       and value.get("source_message_id") == source_id for key, value in receipts.items())
                or source_id in state.get("successor_issues", {})):
            raise RuntimeError("parent_handoff_receipt_not_eligible")
        _, issue = guard.load_issue_record(path, expected_subject_id=SUBJECT_ID,
                                          expected_tuple_sha256=tuple_sha256)
        if (issue.get("request_message_id") != source_id or issue.get("issue_id") != issue_id
                or issue.get("review_artifact_sha256") != tuple_sha256
                or "reviewer_designation" in issue):
            raise RuntimeError("parent_handoff_issue_scope_changed")
        intent = _operator_intent(guard, bridge, operator_intent_message_id)

        def revalidate():
            _, current_issue = guard.load_issue_record(path, expected_subject_id=SUBJECT_ID,
                                                       expected_tuple_sha256=tuple_sha256)
            if current_issue != issue or _operator_intent(guard, bridge, operator_intent_message_id) != intent:
                raise RuntimeError("parent_handoff_binding_changed")
            message = bridge._exact_message(source_id)
            guard._reject_edited_message(message, "handoff_request")
            content = guard._message_identity(message, channel_id=bridge.CHANNEL,
                message_id=source_id, author_id=bridge.KAEL_BOT_ID, role="handoff_request")
            classified = bridge.classify_message(message)
            if (content != issue["request_content"] or message.get("timestamp") != issue["request_timestamp"]
                    or classified is None or classified[0] != "review_request"
                    or bridge.top_stakes_category(*classified) is None):
                raise RuntimeError("parent_handoff_request_changed")
            deferred = bridge._exact_message(DEFERRED_ID)
            guard._reject_edited_message(deferred, "handoff_deferral")
            body = guard._message_identity(deferred, channel_id=bridge.CHANNEL,
                message_id=DEFERRED_ID, author_id=bridge.SELF_ID, role="handoff_deferral")
            if (deferred.get("author", {}).get("bot") is not True
                    or deferred.get("webhook_id") is not None
                    or (deferred.get("message_reference") or {}).get("message_id") != source_id
                    or len(body) != receipt["reply_chars"] or guard.sha256_text(body) != receipt["reply_sha256"]
                    or body != bridge.review_action(*classified, source_message_id=source_id)[1]):
                raise RuntimeError("parent_handoff_deferral_changed")
            bridge._only_expected_replies(source_id, {DEFERRED_ID})
            # Explicit Fable route: never consult request designation or Astra selection.
            if (bridge._resolve_model() != BACKEND[1:] or bridge.SESSION_ID != LEGACY_SESSION_ID
                    or bridge._native_session_id(bridge.SESSION_ID) != NATIVE_SESSION_ID):
                raise RuntimeError("parent_handoff_designated_reviewer_changed")
            return classified

        kind, text = revalidate()
        snapshot = bridge.refresh_included_capacity()
        if not bridge._included_quota_available(snapshot):
            raise RuntimeError("fresh_positive_included_capacity_required")
        review_receipt = {}
        with _host_admission():
            revalidate()  # Host measurement/capacity wait is not authority.
            claim: dict[str, Any] = dict(intent, status="reserved", issue_id=issue_id, tuple_sha256=tuple_sha256,
                         original_deferred_message_id=DEFERRED_ID, attempts=[], posting="not_prepared",
                         nonce=source_id + "-p1")
            receipt["parent_handoff"] = claim
            bridge.save_state(state)  # Any claim is terminal, including a crash before work.

            def before_invoke():
                revalidate()
                if not bridge._included_quota_available(snapshot):
                    raise RuntimeError("fresh_positive_included_capacity_required")
                claim.update(status="invocation_reserved", attempts=review_receipt.get("attempts", []))
                bridge.save_state(state)

            output, exhausted = bridge._invoke_reviewer(BACKEND, bridge._model_prompt(kind, issue["request_content"]),
                review_receipt, time.monotonic() + 1200, capacity_snapshot=snapshot,
                before_invoke=before_invoke, parent_handoff=True)
            claim["attempts"] = review_receipt.get("attempts", [])
            if (exhausted or len(claim["attempts"]) != 1
                    or claim["attempts"][0].get("status") != "ok"
                    or claim["attempts"][0].get("invoked") is not True
                    or not output.strip()):
                claim["status"] = "no_safe_inference"
                bridge.save_state(state)
                raise RuntimeError("parent_handoff_no_safe_inference")
        # Do not tag/strip/restatement-infer a verdict. Preserve raw stdout bytes.
        raw = output.encode("utf-8")
        claim.update(reviewer_identity="fable-xhigh", output_sha256=hashlib.sha256(raw).hexdigest(),
                     output_bytes=len(raw), status="reply_prepared", posting="prepared")
        attach = output != output.strip() or len(output.encode("utf-16-le")) // 2 > 1399
        reply = (f"Verbatim independent reviewer response attached ({len(raw)} UTF-8 bytes; "
                 f"SHA-256: {claim['output_sha256']}). No transport-generated decision.") if attach else output
        original_stdout = output if attach else None
        bridge.save_state(state)
        try:
            revalidate()
        except Exception:
            claim.update(status="reply_withheld_reconciliation", posting="withheld")
            bridge.save_state(state)
            raise
        try:
            posted = bridge.post(reply, reply_to_message_id=source_id, nonce=claim["nonce"],
                                 kind=kind, original_stdout=original_stdout, single_attempt=True)
        except Exception:
            claim.update(status="post_failed_or_ambiguous", posting="failed_or_ambiguous")
            bridge.save_state(state)
            raise
        claim.update(status="posted_unverified", posting="posted_unverified", posted_message_id=posted)
        bridge.save_state(state)
        # No retry on readback failure, including a short-message failure.
        delivered = bridge._exact_message(posted)
        guard._reject_edited_message(delivered, "handoff_reply")
        delivered_content = guard._message_identity(delivered, channel_id=bridge.CHANNEL,
            message_id=posted, author_id=bridge.SELF_ID, role="handoff_reply")
        if (delivered_content != reply or delivered.get("author", {}).get("bot") is not True
                or delivered.get("webhook_id") is not None
                or (delivered.get("message_reference") or {}).get("message_id") != source_id):
            raise RuntimeError("parent_handoff_post_readback_changed")
        verified = bridge.verify_oversize_post(posted, source_id, reply, kind, original_stdout=original_stdout)
        if verified:
            claim["attachment_verification"] = verified
        claim.update(status="posted", posting="posted")
        bridge.save_state(state)
        return {"source_message_id": source_id, "posted_message_id": posted,
                "reviewer_identity": "fable-xhigh", "status": "posted"}
