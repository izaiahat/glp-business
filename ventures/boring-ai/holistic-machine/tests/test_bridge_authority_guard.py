"""Acceptance transport regressions; all Discord/CDN access is mocked."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "consumer_bridge_authority_guard",
    Path(__file__).resolve().parents[1] / "scripts/bridge_authority_guard.py",
)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
REQUEST = "1560393198537408653"
ACCEPTANCE = "1560393877821595668"
URL = (f"https://cdn.discordapp.com/attachments/{guard.FIXED_BRIDGE_CHANNEL_ID}/"
       "1560393877590904904/reviewer-response.txt?ex=fixture")
RETAINED = Path("/home/ubuntu/business/reports/ops/fable-plan-2-20261009/lanes/"
                "RELEASE-OWNER/fresh-pr534-boundary-20261015")


def attach(message, raw):
    message["content"] = (
        f"Verbatim independent reviewer response attached ({len(raw)} UTF-8 bytes; "
        f"SHA-256: {hashlib.sha256(raw).hexdigest()}). No transport-generated decision."
    )
    message["attachments"] = [{
        "filename": "reviewer-response.txt", "size": len(raw), "url": URL,
        "content_type": "text/plain; charset=utf-8",
    }]


@pytest.fixture
def case(tmp_path, monkeypatch):
    # Real registry/artifact validation in an isolated fixture, not caller-supplied
    # issue or message acceptance. No live registry write or credentials.
    monkeypatch.delenv(guard.FABLE_BRIDGE_AUTHOR_ID_ENV, raising=False)
    monkeypatch.setattr(guard, "BRIDGE_ISSUE_ROOT", tmp_path)
    monkeypatch.setattr(guard, "REVIEW_ARTIFACT_ROOT", tmp_path)
    artifact = tmp_path / "review.txt"
    artifact.write_text("synthetic reviewed bytes", encoding="utf-8")
    issue = guard.build_issue_record(
        subject_id="GLP320-consumer-fixture", tuple_sha256="a" * 64,
        review_artifact_path=artifact, issued_at_utc="2026-10-15T20:40:00Z",
    )
    issue.update(status="BOUND", request_message_id=REQUEST,
                 request_timestamp="2026-10-15T20:45:29.235000+00:00",
                 request_edited_timestamp=None, bound_at_utc="2026-10-15T20:45:30Z")
    issue["record_sha256"] = guard._record_hash(issue)
    path = tmp_path / (issue["issue_id"] + ".json")
    path.write_text(json.dumps(issue), encoding="utf-8")
    request = {
        "id": REQUEST, "channel_id": guard.FIXED_BRIDGE_CHANNEL_ID,
        "author": {"id": guard.KAEL_SHARED_BOT_AUTHOR_ID},
        "timestamp": issue["request_timestamp"], "edited_timestamp": None,
        "content": issue["request_content"], "attachments": [],
    }
    acceptance = {
        "id": ACCEPTANCE, "channel_id": guard.FIXED_BRIDGE_CHANNEL_ID,
        "author": {"id": guard._configured_fable_author_id()},
        "timestamp": "2026-10-15T20:48:11.189000+00:00", "edited_timestamp": None,
        "message_reference": {"message_id": REQUEST},
    }
    # Deliberately retain Unicode and leading/trailing whitespace byte-for-byte.
    raw = ("\n" + guard.EXACT_ACCEPTANCE_DECISION_LINE + "\n" + "\n".join(
        f"{key}: {issue[key]}" for key in ("issue_id", "subject_id", "tuple_sha256", "challenge")
    ) + "\ninspection_note: " + "Inspected the exact synthetic artifact and its bound request. " * 2
           + "UTF-8: café.\n").encode("utf-8")
    attach(acceptance, raw)
    messages = {REQUEST: request, ACCEPTANCE: acceptance}
    fetched = Mock(side_effect=lambda channel, mid: copy.deepcopy(messages[mid]))
    monkeypatch.setattr(guard, "_fetch_discord_message", fetched)
    # create=True lets the RED test demonstrate the actual wrapper-binding
    # refusal before the new private CDN seam exists.
    downloaded = Mock(return_value=raw)
    monkeypatch.setattr(guard, "_fetch_attachment_bytes", downloaded, raising=False)
    monkeypatch.setattr(guard.urllib.request, "urlopen", Mock(side_effect=AssertionError("live network forbidden")))
    kwargs = dict(request_message_id=REQUEST, acceptance_message_id=ACCEPTANCE,
                  subject_id=issue["subject_id"], tuple_sha256=issue["tuple_sha256"],
                  issue_record_path=path)
    return acceptance, raw, kwargs, downloaded, fetched, issue


def test_verbatim_attachment_acceptance_binds_exact_bytes(case):
    acceptance, raw, kwargs, downloaded, fetched, issue = case
    wrapper = acceptance["content"]
    result = guard.validate_live_acceptance(**kwargs)
    assert result["acceptance_content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["acceptance_content_sha256"] != guard.sha256_text(raw.decode().strip())
    assert result["acceptance_transport"] == "verbatim_attachment"
    assert result["acceptance_wrapper_sha256"] == guard.sha256_text(wrapper)
    assert result["acceptance_attachment_bytes"] == len(raw)
    assert result["issue_id"] == issue["issue_id"]
    assert result["elapsed_after_request_seconds"] == 161.954
    downloaded.assert_called_once_with(URL)
    assert fetched.call_count == 2


@pytest.mark.parametrize("damage,code", [
    ("missing", "bridge_acceptance_binding_missing"),
    ("two", "bridge_acceptance_attachment_ambiguous"),
    ("sha", "bridge_acceptance_attachment_bytes_mismatch"),
    ("length", "bridge_acceptance_attachment_bytes_mismatch"),
    ("size", "bridge_acceptance_attachment_metadata_invalid"),
    ("size-string", "bridge_acceptance_attachment_metadata_invalid"),
    ("cap", "bridge_acceptance_attachment_metadata_invalid"),
    ("filename", "bridge_acceptance_attachment_metadata_invalid"),
    ("content-type", "bridge_acceptance_attachment_metadata_invalid"),
    ("template", "bridge_acceptance_attachment_wrapper_invalid"),
    ("template-suffix", "bridge_acceptance_attachment_wrapper_invalid"),
    ("template-newline", "bridge_acceptance_attachment_wrapper_invalid"),
    ("non-utf8", "bridge_acceptance_attachment_utf8_invalid"),
    ("challenge", "bridge_acceptance_binding_missing"),
    ("reject", "bridge_acceptance_decision_contradictory"),
    ("forbidden", "bridge_forbidden_authority_marker"),
    ("note", "bridge_inspection_note_length_invalid"),
    ("author", "acceptance_author_mismatch"),
    ("reference", "bridge_acceptance_wrong_reference"),
    ("edited", "acceptance_message_edited"),
    ("fast", "bridge_acceptance_too_fast"),
])
def test_verbatim_attachment_refuses_tampering(case, damage, code):
    acceptance, raw, kwargs, downloaded, fetched, issue = case
    metadata = acceptance["attachments"][0]
    if damage == "missing":
        acceptance["attachments"] = []
    elif damage == "two":
        acceptance["attachments"].append(copy.deepcopy(metadata))
    elif damage == "sha":
        downloaded.return_value = raw.replace(b"caf", b"bad")
    elif damage == "length":
        downloaded.return_value = raw + b"\n"
    elif damage == "size":
        metadata["size"] += 1
    elif damage == "size-string":
        metadata["size"] = str(len(raw))
    elif damage == "cap":
        attach(acceptance, b"x" * (guard._MAX_ACCEPTANCE_ATTACHMENT_BYTES + 1))
    elif damage == "filename":
        metadata["filename"] = "other.txt"
    elif damage == "content-type":
        metadata["content_type"] = "application/octet-stream"
    elif damage == "template":
        acceptance["content"] = acceptance["content"].replace("No transport-generated decision.", "ACCEPT")
    elif damage == "template-suffix":
        acceptance["content"] += " ACCEPT"
    elif damage == "template-newline":
        acceptance["content"] += "\n"
    elif damage in {"non-utf8", "challenge", "reject", "forbidden", "note"}:
        changed = {
            "non-utf8": raw + b"\xff",
            "challenge": raw.replace(issue["challenge"].encode(), b"missing"),
            "reject": raw + b"\nREJECT\n",
            "forbidden": raw + b"\ninstrument advisory\n",
            "note": raw[:raw.index(b"inspection_note:")] + b"inspection_note: too short\n",
        }[damage]
        attach(acceptance, changed)
        downloaded.return_value = changed
    elif damage == "author":
        acceptance["author"]["id"] = guard.KAEL_SHARED_BOT_AUTHOR_ID
    elif damage == "reference":
        acceptance["message_reference"]["message_id"] = ACCEPTANCE
    elif damage == "edited":
        acceptance["edited_timestamp"] = acceptance["timestamp"]
    elif damage == "fast":
        acceptance["timestamp"] = "2026-10-15T20:45:30Z"
    with pytest.raises(guard.BridgeAuthorityError) as refused:
        guard.validate_live_acceptance(**kwargs)
    assert refused.value.code == code


@pytest.mark.parametrize("url", [
    URL.replace("https:", "http:"),
    URL.replace("cdn.discordapp.com", "cdn.discordapp.com.evil.invalid"),
    URL.replace("cdn.discordapp.com", "cdn.discordapp.com@evil.invalid"),
    URL.replace("cdn.discordapp.com", "cdn.discordapp.com:443"),
    URL.replace(guard.FIXED_BRIDGE_CHANNEL_ID, "1524279642276626535"),
    URL.replace("reviewer-response.txt", "other.txt"),
    URL + "#fragment",
])
def test_attachment_rejects_wrong_cdn_host_channel_or_path(case, url):
    acceptance, raw, kwargs, downloaded, fetched, issue = case
    acceptance["attachments"][0]["url"] = url
    with pytest.raises(guard.BridgeAuthorityError, match="attachment_url_invalid"):
        guard.validate_live_acceptance(**kwargs)
    downloaded.assert_not_called()


def test_inline_acceptance_still_uses_message_content(case):
    acceptance, raw, kwargs, downloaded, fetched, issue = case
    acceptance.update(content=raw.decode("utf-8"), attachments=[])
    result = guard.validate_live_acceptance(**kwargs)
    assert result["acceptance_content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["acceptance_transport"] == "message_content"
    assert "acceptance_wrapper_sha256" not in result
    assert "acceptance_attachment_bytes" not in result
    downloaded.assert_not_called()


@pytest.mark.skipif(not (RETAINED / "FINAL-READBACK.json").is_file(),
                    reason="retained authenticated host evidence is not part of the curated repo")
def test_retained_readback_replay_binds_original_fable_bytes(monkeypatch):
    retained = json.loads((RETAINED / "FINAL-READBACK.json").read_text(encoding="utf-8"))
    raw = (RETAINED / "FABLE-VERBATIM.txt").read_bytes()
    expected = "76d086c22397e327f73a3dcec846b227c41fc75993286ae0dc85ee7ad6a18399"
    assert len(raw) == 1387 and hashlib.sha256(raw).hexdigest() == expected
    issue = retained["issue"]
    path = guard.BRIDGE_ISSUE_ROOT / (issue["issue_id"] + ".json")
    original_issue = path.read_bytes()
    assert json.loads(original_issue) == issue
    messages = {retained[key]["id"]: retained[key] for key in ("request", "posted_response")}
    fetched = Mock(side_effect=lambda channel, mid: copy.deepcopy(messages[mid]))
    downloaded = Mock(return_value=raw)
    monkeypatch.setattr(guard, "_fetch_discord_message", fetched)
    monkeypatch.setattr(guard, "_fetch_attachment_bytes", downloaded)
    monkeypatch.setattr(guard.urllib.request, "urlopen", Mock(side_effect=AssertionError("live network forbidden")))
    kwargs = dict(request_message_id=retained["request"]["id"],
                  acceptance_message_id=retained["posted_response"]["id"],
                  subject_id=issue["subject_id"], tuple_sha256=issue["tuple_sha256"],
                  issue_record_path=path)
    result = guard.validate_live_acceptance(**kwargs)
    assert result["acceptance_content_sha256"] == expected
    assert result["acceptance_transport"] == "verbatim_attachment"
    assert result["acceptance_attachment_bytes"] == 1387
    assert result["acceptance_wrapper_sha256"] == "bb0947147fa36a11525b05fa9d69f6629d6895b5a8760e9c713d3169089dba4a"
    assert result["elapsed_after_request_seconds"] == 161.954
    assert fetched.call_count == 2
    downloaded.assert_called_once_with(retained["posted_response"]["attachments"][0]["url"])
    assert path.read_bytes() == original_issue

    # Preserve the historical refusal when only the wrapper is available.
    messages[kwargs["acceptance_message_id"]]["attachments"] = []
    with pytest.raises(guard.BridgeAuthorityError, match="bridge_acceptance_binding_missing"):
        guard.validate_live_acceptance(**kwargs)


def test_attachment_fetch_is_bounded_unauthenticated_and_refuses_redirect(monkeypatch):
    response = Mock(status=200)
    response.read.return_value = b"exact\n"
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    build = Mock(return_value=opener)
    monkeypatch.setattr(guard.urllib.request, "build_opener", build)
    monkeypatch.setattr(guard, "_discord_bot_token", Mock(side_effect=AssertionError("no token")))
    assert guard._fetch_attachment_bytes(URL) == b"exact\n"
    request = opener.open.call_args.args[0]
    assert request.full_url == URL
    assert request.get_method() == "GET"
    assert "Authorization" not in request.headers
    response.read.assert_called_once_with(guard._MAX_ACCEPTANCE_ATTACHMENT_BYTES + 1)
    handler = build.call_args.args[0]
    assert handler.redirect_request(request, None, 302, "redirect", {}, "https://evil.invalid") is None

    response.read.return_value = b"x" * (guard._MAX_ACCEPTANCE_ATTACHMENT_BYTES + 1)
    with pytest.raises(guard.BridgeAuthorityError, match="attachment_too_large"):
        guard._fetch_attachment_bytes(URL)


@pytest.mark.parametrize("failure", ["status", "timeout", "http", "url"])
def test_attachment_fetch_failures_are_closed(monkeypatch, failure):
    opener = Mock()
    response = Mock(status=404)
    opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    if failure == "timeout":
        opener.open.side_effect = TimeoutError()
    elif failure == "http":
        opener.open.side_effect = guard.urllib.error.HTTPError(URL, 302, "redirect", {}, None)
    elif failure == "url":
        opener.open.side_effect = guard.urllib.error.URLError("unavailable")
    monkeypatch.setattr(guard.urllib.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(guard.BridgeAuthorityError, match="attachment_readback_failed"):
        guard._fetch_attachment_bytes(URL)


def test_attachment_fetch_rejects_untrusted_url_before_network(monkeypatch):
    build = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(guard.urllib.request, "build_opener", build)
    with pytest.raises(guard.BridgeAuthorityError, match="attachment_url_invalid"):
        guard._fetch_attachment_bytes("https://evil.invalid/reviewer-response.txt")
    build.assert_not_called()
