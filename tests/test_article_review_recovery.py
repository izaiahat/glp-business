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


def test_actual_cli_admits_under_main_release_lock_and_refuses_contention(tmp_path, monkeypatch):
    import fcntl
    import glp_release_main as main_release

    # Real maintained release module and real kernel flock; no invented lock API.
    assert Path(main_release.__file__).resolve() == Path(__file__).resolve().parents[1] / 'scripts/glp_release_main.py'
    runs = tmp_path / 'registry-site-sync'
    monkeypatch.setattr(main_release, 'RUNS', runs)
    entry = tmp_path / 'entry.json'; entry.write_text('{}')
    history = tmp_path / 'history'; history.mkdir()
    packet = tmp_path / 'packet'; packet.mkdir()
    binding = tmp_path / 'binding.json'; binding.write_text('{}')
    snapshot = tmp_path / 'snapshot.html'; snapshot.write_text('')
    argv = ['article_review_recovery.py', '--entry', str(entry), '--history', str(history),
            '--packet', str(packet), '--live-binding', str(binding), '--live-snapshot', str(snapshot)]
    monkeypatch.setattr(sys, 'argv', argv)
    # The compiler is source-only; resolve it from this checkout even on hosted CI.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'systems/seo-blog-machine/scripts'))
    real_seal = recovery.seal
    admitted = []

    def observe_seal(*args, **kwargs):
        with (runs / 'run.lock').open('a') as competitor:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        admitted.append((args, kwargs))
        return real_seal(*args, **kwargs)

    monkeypatch.setattr(recovery, 'seal', observe_seal)
    # Exercise actual document admission, stopping before any recovery output.
    with pytest.raises(ValueError, match='^exact_single_review_required$'):
        recovery.main()
    assert admitted == [((entry, history, packet, binding, snapshot),
                         {'source_author_packet': None, 'source_author_review': None})]
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with (runs / 'run.lock').open('a') as competitor:
        # Admission failure released the real lock; keep it held for the CLI refusal.
        fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(sys, 'argv', [v if v not in {str(entry), str(history), str(packet), str(binding), str(snapshot)}
                                        else str(tmp_path / 'absent') for v in argv])
        with pytest.raises(BlockingIOError):
            recovery.main()
        assert len(admitted) == 1  # refused before even resolving nonexistent inputs
        fcntl.flock(competitor, fcntl.LOCK_UN)
    with (runs / 'run.lock').open('a') as competitor:
        fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


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
    expected = b'<a href="mailto:support@glpclinicscout.com">support@glpclinicscout.com</a>'
    # The owning comparator refuses even a recognized anchor if its decoder is
    # absent; the former broad helper incorrectly admitted this partial shape.
    with pytest.raises(RuntimeError, match='decoder_script_count_invalid'):
        recovery.require_live_matches_sealed(text.encode(), expected)
    with pytest.raises(ValueError, match='reviewed_live_deployment_drift'):
        recovery.require_live_matches_sealed(b'<script>unsafe()</script>', b'')


def page(body, meta='Learn how', answer='Start with a clinician. [S1]', disclosure='Not medical advice.',
         h1='How to get oral GLP-1'):
    return (f'<head><meta name="description" content="{meta}"></head><main><nav class="crumbs">shell</nav>'
            f'<section class="guide-hero"><h1>{h1}</h1><picture>hero</picture></section>'
            '<p class="guide-evidence-band">Sources checked 2026-10-09</p>'
            f'<section class="answer"><div class="quick-answer"><p class="guide-lede">{answer}</p></div></section>'
            f'<aside class="disclosure"><strong>Evidence boundary.</strong> {disclosure}</aside>'
            f'<nav class="guide-toc">x</nav>{body}'
            '<section class="guide-sources" id="sources"><ol></ol></section><footer>shell</footer></main>')


def test_live_projection_matches_candidate_only_on_reviewed_article_region():
    candidate = page('<p>Prices checked 2026-10-09.</p>')
    live = page('<p>Prices current as of 2026-10-09.</p>').replace('shell', 'different shell').replace('hero', 'other hero')
    assert recovery.article_projection(live) == recovery.article_projection(candidate)
    with pytest.raises(AssertionError):
        assert recovery.article_projection(page('<p>Unreviewed claim.</p>')) == recovery.article_projection(candidate)
    assert recovery.article_projection(page('<p>x</p>', meta='other')) != recovery.article_projection(page('<p>x</p>'))
    with pytest.raises(ValueError, match='article_projection_missing'):
        recovery.article_projection('<html>no article</html>')


def test_projection_covers_h1_quick_answer_and_disclosure():
    """CR-03: content before the TOC is reviewed content, not shell."""
    base = recovery.article_projection(page('<p>x</p>'))
    assert recovery.article_projection(page('<p>x</p>', answer='Buy without a prescription. [S1]')) != base
    assert recovery.article_projection(page('<p>x</p>', disclosure='')) != base
    assert recovery.article_projection(page('<p>x</p>', h1='Different headline')) != base
    banded = page('<p>x</p>').replace('Sources checked 2026-10-09', 'Sources checked 2026-01-01')
    assert recovery.article_projection(banded) != base
    # Removing the quick answer or the disclosure entirely fails closed.
    no_answer = page('<p>x</p>').replace('<section class="answer">', '<section class="lead">')
    no_disclosure = page('<p>x</p>').replace('<aside class="disclosure">', '<aside>')
    for broken in (no_answer, no_disclosure):
        with pytest.raises(ValueError, match='article_projection_missing'):
            recovery.article_projection(broken)


def test_live_quick_answer_or_disclosure_change_refuses_seal():
    """CR-03 through the oracle's seal comparison (used by validate_documents):
    only the live quick answer, disclosure or H1 differs => refusal."""
    candidate = page('<p>Reviewed body.</p>')
    recovery.require_live_matches_candidate(page('<p>Reviewed body.</p>').replace('shell', 'new shell'), candidate)
    for live in (page('<p>Reviewed body.</p>', answer='Changed health answer.'),
                 page('<p>Reviewed body.</p>', disclosure='Removed boundary.'),
                 page('<p>Reviewed body.</p>', h1='Other H1')):
        with pytest.raises(ValueError, match='live_differs_from_reviewed_candidate'):
            recovery.require_live_matches_candidate(live, candidate)
    source = (Path(recovery.__file__)).read_text()
    assert 'require_live_matches_candidate(live_file.read_text(), (fresh / CANDIDATE).read_text())' in source


def test_known_full_build_markup_mismatch_stays_fail_closed():
    """The r2 candidate lacked full-build table wrappers; the oracle is not weakened."""
    candidate = page('<table><tr><td>x</td></tr></table>')
    live = page('<div class="table-scroll"><table><tr><td data-label="A">x</td></tr></table></div>')
    with pytest.raises(ValueError, match='live_differs_from_reviewed_candidate'):
        recovery.require_live_matches_candidate(live, candidate)


def test_ten_subject_publication_context_is_bound_to_original_and_candidate(tmp_path, monkeypatch):
    """Offline shape of the actual ten-subject successor; no runtime acceptance."""
    monkeypatch.setattr(recovery, 'ROOT', tmp_path)
    import hashlib
    original, fresh, rows = build(tmp_path)
    buffer = tmp_path/'reports/glp-clinic-scout/seo-machine/article-buffer'
    registered = buffer/'entries'/('a'*24)/'packet'
    registered.parent.mkdir(parents=True)
    original.rename(registered); original = registered
    claim = buffer/'claims'/('b'*64+'.json'); claim.parent.mkdir(parents=True)
    claim.write_text('{}')
    release = tmp_path/'reports/glp-clinic-scout/seo-machine/current-release-head'
    release.mkdir(parents=True)
    def store(path, value):
        path.write_text(json.dumps(value)); return recovery.spec(path)
    terminal = store(release/'10-final-publication-receipt.json', {
        'schema': 'glp-opus-production-terminal-receipt-v1', 'status': 'PASS_PRODUCTION_TERMINAL',
        'site_head': 'head', 'deployment': {'deployment_id': 'deployment'}})
    current = store(release/'CURRENT-RECEIPT.json', {
        'status': 'PASS_PRODUCTION_TERMINAL', 'site_head': 'head', 'stages': {
            'pages_deploy_intent': {'created_at_utc': '2026-10-12T03:15:22Z', 'commit_hash': 'head'},
            'pages_deployment': {'deployment_id': 'deployment', 'commit_hash': 'head'},
            'route_probe_raw': [{'headers': {'Date': 'Mon, 12 Oct 2026 03:17:31 GMT'}}]}})
    predecessor = store(release/'01-deployment-manifest.json', {'site_head': 'head'})
    consumed = {'schema': 'glp-seo-article-buffer-consumption-v1', 'status': 'PASS_BUFFER_PUBLISHED',
        'entry_id': 'a'*24, 'claim': recovery.spec(claim), 'publication': {
            'receipt': terminal['path'], 'deployment_id': 'deployment'}}
    consumed['consumption_sha256'] = hashlib.sha256((json.dumps(
        consumed, sort_keys=True, separators=(',', ':'), ensure_ascii=False)+'\n').encode()).hexdigest()
    consumed_path = buffer/'consumed'/claim.name; consumed_path.parent.mkdir()
    consumed_path.write_text(json.dumps(consumed))
    headers = tmp_path/'reports/live-guide.headers'
    headers.write_text('HTTP/2 200\ndate: Mon, 12 Oct 2026 03:17:31 GMT\n')
    context = {'scope': 'POSTPUBLICATION_RECOVERY_REVIEW_ONLY_NOT_NEW_PUBLICATION',
        'no_invocation_authority': True, 'original_consumption': consumed,
        'original_terminal': json.loads(Path(terminal['path']).read_text()),
        'fresh_native_build': {'status': 'PASS_NATIVE_BUILD', 'returncode': 0,
                              'candidate': recovery.spec(fresh/recovery.CANDIDATE)},
        'first_publication': {'not_before_utc': '2026-10-12T03:15:22Z',
            'observed_live_by_utc': '2026-10-12T03:17:31Z', 'date': '2026-10-11',
            'deployment_id': 'deployment', 'site_head': 'head', 'timezone': 'America/Los_Angeles',
            'evidence': [{'path': str(Path(s['path']).relative_to(tmp_path)), 'sha256': s['sha256']}
                         for s in (current, terminal, recovery.spec(consumed_path), recovery.spec(headers))]}}
    context_path = fresh/'09-publication-context.json'
    def check_context(value):
        context_path.write_text(json.dumps(value))
        manifest = manifest_for(fresh)
        manifest['subject_files'].append({'path': context_path.name, 'sha256': recovery.sha(context_path)})
        return recovery.successor_contract(original, fresh, rows, manifest,
                                           compile_packet=fake_compile)
    assert check_context(context)['publication_context_sha256'] == recovery.sha(context_path)
    unexpected = manifest_for(fresh)
    unexpected['subject_files'].append({'path': context_path.name, 'sha256': recovery.sha(context_path)})
    unexpected['subject_files'].append({'path': '10-unreviewed.json', 'sha256': '0'*64})
    with pytest.raises(ValueError, match='recovery_subject_set'):
        recovery.successor_contract(original, fresh, rows, unexpected, compile_packet=fake_compile)
    bad = copy.deepcopy(context); bad['fresh_native_build']['candidate']['sha256'] = '0'*64
    with pytest.raises(ValueError, match='publication_context_candidate_binding'):
        check_context(bad)
    bad = copy.deepcopy(context); bad['original_consumption']['entry_id'] = 'c'*24
    with pytest.raises(ValueError, match='publication_context_original_consumption'):
        check_context(bad)
    bad = copy.deepcopy(context); bad['no_invocation_authority'] = False
    with pytest.raises(ValueError, match='publication_context_scope'):
        check_context(bad)
    bad = copy.deepcopy(context); bad['first_publication']['date'] = '2026-10-12'
    with pytest.raises(ValueError, match='publication_context'):
        check_context(bad)
    (release/'CURRENT-RECEIPT.json').write_text('{}')
    with pytest.raises(ValueError):
        check_context(context)


def _retained_successor_adoption_fixture(tmp_path, monkeypatch, fault):
    """Source IO regression with explicit authentication doubles, never model evidence."""
    import hashlib, subprocess
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'systems/analytics-brain'))
    import glp_seo_updater_source as updater
    assert Path(updater.__file__).resolve() == Path(__file__).resolve().parents[1] / 'scripts/glp_seo_updater_source.py'
    import fcntl
    from carrier_lock import carrier_lock
    lock_path = tmp_path / 'private-carrier.lock'
    # Real production flock, isolated destination only; never a live lease.
    monkeypatch.setattr(updater, 'carrier_lock', lambda: carrier_lock(lock_path))
    def assert_owner_held():
        with lock_path.open('r') as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    save = updater.save
    def locked_save(*args, **kwargs):
        assert_owner_held()
        return save(*args, **kwargs)
    monkeypatch.setattr(updater, 'save', locked_save)
    workspace = tmp_path / 'site'; workspace.mkdir()
    subprocess.run(['git', 'init', '-q', workspace], check=True)
    packet = tmp_path / 'packet'; packet.mkdir()
    history = tmp_path / 'history'; history.mkdir()
    entry_path = tmp_path / 'entry.json'
    entry_path.write_text(json.dumps({'target_route': '/guides/test/', 'article_id': 'test'}))
    context = {'source_head': 'a'*40, 'first_publication': {'date': '2026-10-11', 'timezone': 'America/Los_Angeles'}}
    (packet / '09-publication-context.json').write_text(json.dumps(context))
    for name, value in {'06-article.json': {'article_id': 'test', 'canonical_path': '/guides/test/'},
                        '07-qa-receipt.json': {'fixture': 'reviewed QA'}}.items():
        (packet / name).write_text(json.dumps(value))
    review = history / 'round-01-receipt.json'; review.write_text('{"fixture":"reviewed receipt"}')
    base = workspace / 'data/blog/accepted-guides/test'; base.mkdir(parents=True)
    row = {'route': '/guides/test/', 'entry_id': 'retained', 'before_sha256': 'unchanged',
           'weekly_strategy_sha256': None, 'semantic_materiality': {'material': True},
           'first_publication': context['first_publication']}
    for key in ('article', 'qa', 'review'):
        file = base / (key + '.json')
        file.write_text(json.dumps({'article_id': 'test', 'canonical_path': '/guides/test/', 'predecessor': key}))
        row[key] = {'path': file.relative_to(workspace).as_posix(), 'sha256': recovery.sha(file)}
    other = {'route': '/guides/untouched/', 'fixture': 'untouched'}
    manifest_path = base.parent / 'updates.json'
    manifest_path.write_text(json.dumps({'schema': 'glp-accepted-guide-updates-v1', 'entries': [other, row]}))
    def git(*args):
        return subprocess.check_output(['git', '-C', str(workspace), *args], text=True).strip()
    git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
    head = git('rev-parse', 'HEAD')
    document = {'entry': recovery.spec(entry_path), 'review': recovery.spec(review),
                'candidate': {'path': str(packet / recovery.CANDIDATE)}}
    subject_files = [dict(path=n, sha256=recovery.sha(packet / n))
                     for n in ('06-article.json', '07-qa-receipt.json', recovery.PUBLICATION_CONTEXT)]
    # Independent fixture acceptance is frozen before any coherent mutation.
    # The real retained probe uses unchanged production PR77/RV77 pins instead.
    monkeypatch.setattr(updater, 'ACCEPTED_SUCCESSOR_SOURCE_HEAD', context['source_head'])
    monkeypatch.setattr(updater, 'ACCEPTED_SUCCESSOR_SOURCE_ROW_SHA256',
        hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest())
    monkeypatch.setattr(recovery, 'successor_documents', lambda *a, **kw: document)
    authenticated = []
    def authenticate(*args, **kwargs):
        assert_owner_held()
        authenticated.append((args, kwargs))
        if fault == 'native_refusal':
            raise ValueError('fixture_native_refusal')
        if fault == 'successor_drift':
            (packet / '07-qa-receipt.json').write_text('{}')
        if fault == 'postauth_context_and_source_drift':
            drift = copy.deepcopy(context)
            drift['first_publication']['date'] = '2026-10-12'
            (packet / recovery.PUBLICATION_CONTEXT).write_text(json.dumps(drift))
            row['first_publication'] = drift['first_publication']
            manifest_path.write_text(json.dumps({'schema': 'glp-accepted-guide-updates-v1', 'entries': [other, row]}))
            assert git('status', '--porcelain')
        return {'document': document, 'manifest': {'subject_files': subject_files}}
    monkeypatch.setattr(recovery, 'validate_successor_documents', authenticate)
    if fault in ('wrong_article', 'wrong_qa', 'wrong_review'):
        key = fault.removeprefix('wrong_')
        (workspace / row[key]['path']).write_text('{}')
        git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'wrong preimage')
        head = git('rev-parse', 'HEAD')
    if fault and fault.startswith('rehashed_'):
        key = fault.removeprefix('rehashed_')
        file = workspace / row[key]['path']
        value = json.loads(file.read_text()); value['unaccepted'] = key
        file.write_text(json.dumps(value))
        row[key]['sha256'] = recovery.sha(file)
        manifest_path.write_text(json.dumps({'schema': 'glp-accepted-guide-updates-v1', 'entries': [other, row]}))
        git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'coherent unaccepted preimage')
        head = git('rev-parse', 'HEAD')
        assert not git('status', '--porcelain')
    if fault == 'publication_context':
        context['first_publication']['date'] = '2026-10-12'
        (packet / '09-publication-context.json').write_text(json.dumps(context))
    if fault == 'dirty_source':
        manifest_path.write_text(manifest_path.read_text() + '\n')
    if fault == 'wrong_head':
        head = '0' * 40
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file() and '.git' not in p.parts}
    call = lambda: updater.adopt_successor(workspace, entry_path, packet, history, expected_head=head)
    if fault:
        error = ('successor_source_accepted_preimage_drift' if fault.startswith('rehashed_')
                 else 'successor_source_authenticated_input_drift' if fault == 'postauth_context_and_source_drift'
                 else None)
        with pytest.raises((ValueError, RuntimeError), match=error):
            call()
        if fault == 'postauth_context_and_source_drift':
            # Only the injected row mutation is allowed, never any adopter output.
            before[manifest_path.relative_to(workspace)] = json.dumps(
                {'schema': 'glp-accepted-guide-updates-v1', 'entries': [other, row]}).encode()
        assert before == {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file() and '.git' not in p.parts}
        with lock_path.open('r') as contender:
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    result = call()
    assert authenticated and result['status'] == 'SUCCESSOR_SOURCE_STAGED_NOT_ACCEPTED'
    manifest = json.loads(manifest_path.read_text())
    assert manifest['entries'][0] == other
    adopted = manifest['entries'][1]
    assert {k: v for k, v in adopted.items() if k not in ('article', 'qa', 'review')} == {k: v for k, v in row.items() if k not in ('article', 'qa', 'review')}
    for key, origin in {'article': packet / '06-article.json', 'qa': packet / '07-qa-receipt.json', 'review': review}.items():
        assert adopted[key]['sha256'] == recovery.sha(origin)
        assert (workspace / adopted[key]['path']).read_bytes() == origin.read_bytes()
        assert (workspace / row[key]['path']).read_bytes() == before[Path(row[key]['path'])]


@pytest.mark.parametrize('fault', [None, 'wrong_article', 'wrong_qa', 'wrong_review', 'native_refusal', 'publication_context', 'dirty_source', 'successor_drift', 'wrong_head'])
def test_retained_successor_adoption_exact_source_binding_and_refusal(tmp_path, monkeypatch, fault):
    _retained_successor_adoption_fixture(tmp_path, monkeypatch, fault)


@pytest.mark.parametrize('key', ['article', 'qa', 'review'])
def test_retained_successor_refuses_coherent_committed_preimage(tmp_path, monkeypatch, key):
    """RV506 P1: clean caller head + coherent editable hashes aren't acceptance."""
    _retained_successor_adoption_fixture(tmp_path, monkeypatch, 'rehashed_' + key)


def test_retained_successor_refuses_postauth_context_source_handoff(tmp_path, monkeypatch):
    """RV506 P1: sealed context and original clean preimage survive handoff."""
    _retained_successor_adoption_fixture(tmp_path, monkeypatch, 'postauth_context_and_source_drift')


def test_natural_cannot_be_recovered(tmp_path):
    with pytest.raises(ValueError, match='postpublication_review_not_natural'):
        recovery.validate_recovery(tmp_path/'entry.json', root=tmp_path, natural=True)
