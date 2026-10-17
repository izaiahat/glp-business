"""P1 regressions: offline original-reviewer probes, now asserting refusal."""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path

import pytest

from tests.test_linear_ordinary_completion import (
    completion, readonly_completion, protected_noncode_completion,
    protected_runtime_completion, operator_discord_completion, lo, digest,
)
import linear_operator_acceptance as oa


def bind(data, path, parent, target, reply):
    data['protected_review']['discord'] = oa.capture(target['identifier'], data['evidence'], reply['id'])
    parent['body'] = parent['body'].split('\nIndependent operator Discord acceptance SHA256:')[0] + '\nIndependent operator Discord acceptance SHA256: ' + oa.binding_sha256(data['protected_review']['discord'])
    data['review']['body_sha256'] = digest(parent['body'])
    path.write_text(json.dumps(data))


def run(target, path, data, *, check_only=False):
    runtime = data['scope'] == 'protected-runtime'
    lo.pass_issue(target, path, {'Done': 'done'}, protected_noncode=not runtime,
                  protected_runtime=runtime, check_only=check_only)


def date_messages(request, reply, rows, then):
    def snowflake(stamp):
        return str((int(stamp.timestamp() * 1000) - 1420070400000) << 22)
    request.update(id=snowflake(then), timestamp=then.isoformat())
    later = then + timedelta(minutes=1)
    reply.update(id=snowflake(later), timestamp=later.isoformat())
    reply['message_reference']['message_id'] = request['id']
    rows.clear()
    rows.update({request['id']: request, reply['id']: reply})


@pytest.mark.parametrize('age_days', [0, 365])
def test_reviewer_age_probe(operator_discord_completion, capsys, age_days):
    target, path, data, writes, parent, request, reply, rows = operator_discord_completion
    then = datetime.now(timezone.utc) - timedelta(days=age_days, minutes=2)
    # Isolate the age limit: even evidence older than the approval cannot save it.
    os.utime(data['evidence'][0]['path'], (then.timestamp() - 60,) * 2)
    date_messages(request, reply, rows, then)
    if age_days:
        with pytest.raises((ValueError, SystemExit), match='fresh|7 days'):
            bind(data, path, parent, target, reply)
            run(target, path, data, check_only=True)
    else:
        bind(data, path, parent, target, reply)
        run(target, path, data, check_only=True)
        assert json.loads(capsys.readouterr().out)['validated'] is True
    assert writes == []


def test_reviewer_reuse_after_reopen_probe(operator_discord_completion, capsys):
    target, path, data, writes, parent, request, reply, rows = operator_discord_completion
    run(target, path, data)
    assert [w[0] for w in writes] == ['comment', 'state']
    capsys.readouterr()
    target['state'] = {'name': 'In Progress'}
    parent['createdAt'] = datetime.now(timezone.utc).isoformat()
    with pytest.raises(SystemExit, match='consumed'):
        run(target, path, data)
    assert [w[0] for w in writes] == ['comment', 'state']


@pytest.mark.parametrize('when', ['before_evidence', 'future', 'after_lint'])
def test_attempt_freshness_refuses_before_mutation(operator_discord_completion, monkeypatch, when):
    target, path, data, writes, parent, request, reply, rows = operator_discord_completion
    if when == 'before_evidence':
        stamp = datetime.now(timezone.utc).timestamp()
        os.utime(data['evidence'][0]['path'], (stamp, stamp))
    elif when == 'future':
        then = datetime.now(timezone.utc) + timedelta(minutes=2)
        date_messages(request, reply, rows, then)
    else:
        def late_touch(*a, **kw):
            stamp = datetime.now(timezone.utc).timestamp()
            os.utime(data['evidence'][0]['path'], (stamp, stamp))
        monkeypatch.setattr(lo, '_lint_result', late_touch)
    with pytest.raises((ValueError, SystemExit), match='fresh|evidence|future'):
        if when != 'after_lint':
            bind(data, path, parent, target, reply)
        run(target, path, data)
    assert writes == []


@pytest.mark.parametrize('uncertain', ['comment', 'done'])
def test_uncertain_attempt_burns_reply_before_external_write(operator_discord_completion, monkeypatch, uncertain):
    target, path, data, writes, *_ = operator_discord_completion
    if uncertain == 'comment':
        monkeypatch.setattr(lo, 'comment', lambda *a: {'success': False})
    else:
        def uncertain_done(*a):
            raise RuntimeError('uncertain Done transport')
        monkeypatch.setattr(lo, 'set_state', uncertain_done)
    with pytest.raises((SystemExit, RuntimeError), match='unconfirmed|uncertain'):
        run(target, path, data)
    before = list(writes)
    with pytest.raises(SystemExit, match='consumed'):
        run(target, path, data)
    assert writes == before


def test_check_only_does_not_consume_and_new_reply_allows_reopen(operator_discord_completion, capsys):
    target, path, data, writes, parent, request, reply, rows = operator_discord_completion
    run(target, path, data, check_only=True)
    run(target, path, data, check_only=True)
    assert not oa.CONSUMED_DB.exists()
    run(target, path, data)
    target['state'] = {'name': 'In Progress'}
    then = datetime.now(timezone.utc)
    # A new unedited operator reply (not a new coordinator comment) grants another attempt.
    date_messages(request, reply, rows, then - timedelta(minutes=1))
    bind(data, path, parent, target, reply)
    run(target, path, data)
    assert [w[0] for w in writes] == ['comment', 'state', 'comment', 'state']


def test_new_id_cannot_replay_an_old_unconsumed_approval(operator_discord_completion):
    target, path, data, writes, parent, request, reply, rows = operator_discord_completion
    run(target, path, data)
    target['state'] = {'name': 'In Progress'}
    # Different ID and fresh parent do not refresh a human message from before
    # the previous attempt (even within seven days and after evidence mtime).
    then = datetime.now(timezone.utc) - timedelta(seconds=90)
    date_messages(request, reply, rows, then)
    with pytest.raises(ValueError, match='previous closure attempt'):
        bind(data, path, parent, target, reply)
    assert [w[0] for w in writes] == ['comment', 'state']


def test_concurrent_attempts_only_one_can_consume(operator_discord_completion):
    from concurrent.futures import ThreadPoolExecutor
    target, path, data, writes, *_ = operator_discord_completion
    binding = data['protected_review']['discord']
    def attempt(_):
        try:
            oa.consume(binding, target['identifier'], data['evidence'], lo.sha(path))
            return 'consumed'
        except ValueError as exc:
            assert 'consumed' in str(exc) or 'unavailable' in str(exc)
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['consumed', 'refused']
    assert writes == []


def test_consumption_survives_a_new_process(operator_discord_completion):
    import subprocess
    import sys
    target, path, data, writes, *_ = operator_discord_completion
    run(target, path, data)
    probe = ('import sys,json;sys.path.insert(0,sys.argv[1]);'
             'import linear_operator_acceptance as oa;from pathlib import Path;'
             'oa.CONSUMED_DB=Path(sys.argv[2]);'
             'oa._verify_unused(json.loads(sys.argv[3]))')
    result = subprocess.run([sys.executable, '-B', '-c', probe, str(Path(oa.__file__).parent),
                             str(oa.CONSUMED_DB), json.dumps(data['protected_review']['discord'])],
                            capture_output=True, text=True)
    assert result.returncode != 0 and 'already consumed' in result.stderr
    assert [w[0] for w in writes] == ['comment', 'state']


def test_unavailable_consumption_store_refuses_without_writes(operator_discord_completion):
    target, path, data, writes, *_ = operator_discord_completion
    oa.CONSUMED_DB.write_bytes(b'not a SQLite database')
    with pytest.raises(SystemExit, match='consumption unavailable'):
        run(target, path, data)
    assert writes == []
