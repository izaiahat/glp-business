"""Reset exact-subject regressions; fake handles only, no actual redemption."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import capacity_reset_policy as m
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def test_same_label_with_different_subject_after_post_is_uncertain(tmp_path):
    rows = [dict(label='codex-account-2', remaining_pct=0, reset_at_utc='2026-10-18T00:00:00Z',
                 banked_resets=2, identity_safe=True, identity_sha256='a' * 64)]
    posts, cleared = [], []
    def read():
        return copy.deepcopy(rows), {'codex-account-2': {'access_token': 'fixture'}}
    def post(handle, request):
        posts.append(request)
        rows[0].update(remaining_pct=100, banked_resets=1, identity_sha256='b' * 64)
        return {'code': 'reset'}
    assert m.reset_tick(tmp_path, now=NOW, dry_run=False, reader=read, consumer=post,
                        clear=cleared.append)['status'] == 'uncertain'
    assert m.reset_tick(tmp_path, now=NOW, dry_run=False, reader=read, consumer=post,
                        clear=cleared.append)['status'] == 'reconciliation_required'
    assert len(posts) == 1 and not cleared


def test_subject_change_between_plan_and_locked_read_never_posts(tmp_path):
    calls, posts = [], []
    def read():
        calls.append(True)
        return [dict(label='codex-account-2', remaining_pct=0, reset_at_utc='2026-10-18T00:00:00Z',
                     banked_resets=2, identity_safe=True, identity_sha256=('a' if len(calls) == 1 else 'b') * 64)], {
                         'codex-account-2': {'access_token': 'fixture'}}
    result = m.reset_tick(tmp_path, now=NOW, dry_run=False, reader=read,
                          consumer=lambda *a: posts.append(True))
    assert result['status'] == 'changed_before_redemption' and not posts
