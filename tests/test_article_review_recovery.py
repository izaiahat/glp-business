"""Post-publication recovery: successor derivation contract (GLP-320 A5).

Regenerated 06/07 metadata is admitted only through a positively exercised
derivation: identity subjects unchanged, 06 equal to the compiler output of
the sealed sources, 07 a full PASS bound to exactly those bytes. Old QA copied
into the successor can never manufacture equality. No network, no notifier.
"""
import copy
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import article_review_recovery as recovery

SEAL = '2026-10-13T03:00:00Z'


def fake_compile(packet):
    return {'draft': (packet/'04-draft.md').read_text(),
            'render_contract': {'schema': 'glp-accepted-guide-render-v1'}}


def write_article(packet):
    (packet/'06-article.json').write_text(json.dumps(fake_compile(packet), indent=2, sort_keys=True)+'\n')


def write_qa(packet, checked='2026-10-13T02:59:00Z', bound=None):
    names = sorted(recovery.SUBJECTS - {'07-qa-receipt.json'})
    arts = {n: {'sha256': recovery.sha((bound or packet)/n)} for n in names}
    (packet/'07-qa-receipt.json').write_text(json.dumps(
        {'status': 'PASS', 'failures': [], 'checks': {'a': True}, 'checked_at_utc': checked, 'artifacts': arts}))


def build(tmp_path):
    original = tmp_path/'old'; fresh = tmp_path/'new'; original.mkdir(); fresh.mkdir()
    for d, draft in ((original, 'old draft'), (fresh, 'repaired draft')):
        for name in recovery.SUBJECTS - {'06-article.json', '07-qa-receipt.json'}:
            (d/name).write_text(name if name != '04-draft.md' else draft)
        write_article(d); write_qa(d)
    (fresh/recovery.CANDIDATE).write_text('<html>candidate</html>')
    rows = [{'path': n, 'sha256': recovery.sha(original/n)} for n in sorted(recovery.SUBJECTS)]
    return original, fresh, rows


def manifest_for(fresh):
    return {'sealed_at_utc': SEAL, 'subject_files': [
        {'path': n, 'sha256': recovery.sha(fresh/n)} for n in sorted(recovery.SUBJECTS | {recovery.CANDIDATE})]}


def check(original, fresh, rows, manifest=None):
    return recovery.successor_contract(original, fresh, rows, manifest or manifest_for(fresh),
                                       compile_packet=fake_compile)


def test_genuine_successor_with_regenerated_06_07_is_admitted(tmp_path):
    original, fresh, rows = build(tmp_path)
    result = check(original, fresh, rows)
    assert result['source_repairs'] == ['04-draft.md']
    assert result['identity'] == ['00-authority.json', '02-serp-brief.json']
    # The old strict eight-byte equality would have refused this genuine successor.
    assert recovery.sha(fresh/'06-article.json') != recovery.sha(original/'06-article.json')
    assert recovery.sha(fresh/'07-qa-receipt.json') != recovery.sha(original/'07-qa-receipt.json')


def test_identity_subject_change_is_refused(tmp_path):
    original, fresh, rows = build(tmp_path)
    (fresh/'00-authority.json').write_text('retargeted'); write_qa(fresh)
    with pytest.raises(ValueError, match='identity_subject_changed'):
        check(original, fresh, rows)


def test_copied_article_is_refused(tmp_path):
    original, fresh, rows = build(tmp_path)
    (fresh/'06-article.json').write_bytes((original/'06-article.json').read_bytes()); write_qa(fresh)
    with pytest.raises(ValueError, match='successor_article_not_compiled'):
        check(original, fresh, rows)


def test_copied_old_qa_cannot_manufacture_equality(tmp_path):
    original, fresh, rows = build(tmp_path)
    (fresh/'07-qa-receipt.json').write_bytes((original/'07-qa-receipt.json').read_bytes())
    with pytest.raises(ValueError, match='successor_qa_unbound'):
        check(original, fresh, rows)


def test_failed_or_late_qa_is_refused(tmp_path):
    original, fresh, rows = build(tmp_path)
    write_qa(fresh, checked='2026-10-13T03:00:01Z')
    with pytest.raises(ValueError, match='successor_qa_after_seal'):
        check(original, fresh, rows)
    qa = json.loads((fresh/'07-qa-receipt.json').read_text()); qa['checks']['a'] = False
    (fresh/'07-qa-receipt.json').write_text(json.dumps(qa))
    with pytest.raises(ValueError, match='successor_qa_not_PASS'):
        check(original, fresh, rows)


def test_manifest_must_be_exact_successor_bytes_and_candidate(tmp_path):
    original, fresh, rows = build(tmp_path)
    manifest = manifest_for(fresh)
    for tamper in ('missing_candidate', 'extra', 'rehashed'):
        m = copy.deepcopy(manifest)
        if tamper == 'missing_candidate':
            m['subject_files'] = [r for r in m['subject_files'] if r['path'] != recovery.CANDIDATE]
        elif tamper == 'extra':
            m['subject_files'].append({'path': 'other', 'sha256': 'a'*64})
        else:
            m['subject_files'][0]['sha256'] = 'a'*64
        with pytest.raises(ValueError):
            check(original, fresh, rows, m)


def test_cf_normalization_is_narrow():
    key = 42
    cipher = bytes([key])+bytes(x ^ key for x in b'support@glpclinicscout.com')
    text = ('<a href="/cdn-cgi/l/email-protection#'+cipher.hex()+'"><span class="__cf_email__" data-cfemail="'
            + cipher.hex()+'">[email&#160;protected]</span></a>')
    assert recovery.normalize_live(text) == '<a href="mailto:support@glpclinicscout.com">support@glpclinicscout.com</a>'
    assert recovery.normalize_live('<script>unsafe()</script>') == '<script>unsafe()</script>'


def page(body, meta='Learn how'):
    return (f'<head><meta name="description" content="{meta}"></head><main><nav class="guide-toc">x</nav>{body}'
            '<section class="guide-sources" id="sources"><ol></ol></section><footer>shell</footer></main>')


def test_live_projection_matches_candidate_only_on_reviewed_article_region():
    candidate = page('<p>Prices checked 2026-10-09.</p>')
    live = page('<p>Prices current as of 2026-10-09.</p>').replace('shell', 'different shell')
    assert recovery.article_projection(live) == recovery.article_projection(candidate)
    with pytest.raises(AssertionError):
        assert recovery.article_projection(page('<p>Unreviewed claim.</p>')) == recovery.article_projection(candidate)
    assert recovery.article_projection(page('<p>x</p>', meta='other')) != recovery.article_projection(page('<p>x</p>'))
    with pytest.raises(ValueError, match='article_projection_missing'):
        recovery.article_projection('<html>no article</html>')


def test_natural_cannot_be_recovered(tmp_path):
    with pytest.raises(ValueError, match='postpublication_review_not_natural'):
        recovery.validate_recovery(tmp_path/'entry.json', root=tmp_path, natural=True)
