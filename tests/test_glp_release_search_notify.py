"""Offline changed-URL submission, deployment custody and no-blind-replay."""
import hashlib
import json
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_release_search_notify as notify


def test_live_default_callbacks_are_refused_before_any_io(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('live_transport_or_file_io_reached')
    monkeypatch.setattr(notify.urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    with pytest.raises(RuntimeError, match='live_search_effect_refused'):
        notify.submit_deployment({'deployment_id':'real-looking-id'}, Path('/unused'))
    with pytest.raises(RuntimeError, match='live_search_effect_refused'):
        notify.post('https://api.indexnow.org/indexnow', {})
    with pytest.raises(RuntimeError, match='live_search_effect_refused'):
        notify.gsc_submit(b'<urlset/>')
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.setattr(notify, '_under_test_harness', lambda: False)
    with pytest.raises(RuntimeError, match='source_not_installed'):
        notify.submit_deployment({'deployment_id':'id'}, Path('/unused'))
    with pytest.raises(RuntimeError, match='source_not_installed'):
        notify.post('https://api.indexnow.org/indexnow', {})


def test_pytest_process_refuses_even_without_pytest_current_test(monkeypatch):
    """CR-01: PYTEST_CURRENT_TEST is unset during collection/imports; a loaded
    pytest must still refuse, even for the installed source path."""
    def forbidden(*a, **kw):
        raise AssertionError('live_transport_or_file_io_reached')
    monkeypatch.setattr(notify.urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.delenv('PYTEST_VERSION', raising=False)
    with pytest.raises(RuntimeError, match='live_search_effect_refused:pytest'):
        notify.require_installed_effect(source=notify.INSTALLED_SOURCE)
    monkeypatch.setattr(notify, 'INSTALLED_SOURCE', Path(notify.__file__).resolve())
    with pytest.raises(RuntimeError, match='live_search_effect_refused:pytest'):
        notify.post('https://api.indexnow.org/indexnow', {})
    with pytest.raises(RuntimeError, match='live_search_effect_refused:pytest'):
        notify.gsc_submit(b'<urlset/>')
    with pytest.raises(RuntimeError, match='live_search_effect_refused:pytest'):
        notify.submit_deployment({'deployment_id': 'real-looking-id'}, Path('/unused'))


def test_collection_time_call_is_refused_before_io(tmp_path):
    """CR-01 regression: a module-level (collection-time) live call inside a real
    pytest subprocess, with the installed path modelled as this file, refuses
    before any network or file IO."""
    module = tmp_path / 'test_collect_probe.py'
    module.write_text(
        'import os, sys, urllib.request\n'
        f'sys.path.insert(0, {str(ROOT / "scripts")!r})\n'
        'import glp_release_search_notify as n\n'
        'from pathlib import Path\n'
        'assert not os.environ.get("PYTEST_CURRENT_TEST")\n'
        'os.environ.pop("PYTEST_VERSION", None)\n'
        'n.INSTALLED_SOURCE = Path(n.__file__).resolve()\n'
        'def forbidden(*a, **k): raise SystemExit("IO_REACHED")\n'
        'urllib.request.urlopen = forbidden\n'
        'try:\n'
        '    n.post("https://api.indexnow.org/indexnow", {"urlList": []})\n'
        'except RuntimeError as exc:\n'
        '    print("REFUSED:" + str(exc))\n'
        '    raise\n')
    import subprocess
    env = {k: v for k, v in __import__('os').environ.items() if not k.startswith('PYTEST_')}
    run = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', '-s', '-p', 'no:cacheprovider',
                          str(module)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    out = run.stdout + run.stderr
    assert run.returncode != 0
    assert 'live_search_effect_refused:pytest' in out
    assert 'IO_REACHED' not in out


def test_uninstalled_checkout_refuses_outside_pytest(tmp_path):
    """CR-01: a plain (non-pytest) interpreter importing this checkout's module
    is an uninstalled checkout and must refuse before IO."""
    import subprocess
    probe = ('import sys, urllib.request\n'
             f'sys.path.insert(0, {str(ROOT / "scripts")!r})\n'
             'import glp_release_search_notify as n\n'
             'assert "pytest" not in sys.modules and "_pytest" not in sys.modules\n'
             'def forbidden(*a, **k): raise SystemExit("IO_REACHED")\n'
             'urllib.request.urlopen = forbidden\n'
             'for call in (lambda: n.post("https://api.indexnow.org/indexnow", {}), lambda: n.gsc_submit(b"x")):\n'
             '    try: call()\n'
             '    except RuntimeError as exc: print(exc)\n')
    env = {k: v for k, v in __import__('os').environ.items() if not k.startswith('PYTEST_')}
    run = subprocess.run([sys.executable, '-c', probe], env=env, capture_output=True, text=True, timeout=60)
    lines = run.stdout.split()
    expected = ['live_search_effect_refused:source_not_installed'] * 2
    if Path(notify.__file__).resolve() == notify.INSTALLED_SOURCE.resolve():
        pytest.skip('running from the installed checkout itself')
    assert run.returncode == 0 and lines == expected, run.stdout + run.stderr


def _fixture_release(tmp_path):
    old, new = tmp_path/'old', tmp_path/'new'
    for root in (old,new):
        (root/'public/guides/unchanged').mkdir(parents=True)
        (root/'public/guides/unchanged/index.html').write_text('<html>same</html>')
        (root/'public/robots.txt').write_text('User-agent: *\nAllow: /\n')
    (new/'public/guides/new').mkdir()
    (new/'public/guides/new/index.html').write_text('<html>new guide</html>')
    (new/'public/sitemap.xml').write_text('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://glpclinicscout.com/guides/new/</loc></url></urlset>')
    (new/'public/fixture.txt').write_text('fixture')
    (new/'data').mkdir(); (new/'data/indexnow.json').write_text(json.dumps({'host':'glpclinicscout.com','key':'fixture','key_location':'https://glpclinicscout.com/fixture.txt'}))
    final=tmp_path/'final.json'; final.write_text(json.dumps({'status':'PASS_PRODUCTION_TERMINAL','deployment':{'deployment_id':'dep-1'},'site_head':'head-1'}))
    pointer={'deployment_id':'dep-1','site_head':'head-1','release_root':str(new),'supersedes':{'release_root':str(old)},
             'final_receipt':{'path':str(final),'sha256':hashlib.sha256(final.read_bytes()).hexdigest()}}
    return old, new, pointer


def test_intent_is_directory_durable_before_any_provider_call(tmp_path, monkeypatch):
    """CR-02: intent file fsync, rename, and containing-directory fsync (plus every
    newly created parent) all happen before either provider is invoked."""
    old, new, pointer = _fixture_release(tmp_path)
    monkeypatch.setattr(notify,'current_pointer',lambda:pointer)
    monkeypatch.setattr(notify,'live_sitemap',lambda:new.joinpath('public/sitemap.xml').read_bytes())
    events=[]
    real_replace, real_dir = notify.os.replace, notify._fsync_dir
    monkeypatch.setattr(notify.os,'replace',lambda a,b:(events.append(('replace',Path(b).name)),real_replace(a,b))[1])
    monkeypatch.setattr(notify,'_fsync_dir',lambda p:(events.append(('dirsync',Path(p).name)),real_dir(p))[1])
    def post(endpoint,payload): events.append(('indexnow_post',None)); return 200,b''
    def gsc(xml): events.append(('gsc_submit',None)); return {'http_status':204}
    report=tmp_path/'receipts'/'nested'
    notify.submit_deployment(pointer,old,root=tmp_path,report_root=report,ledger=tmp_path/'l.jsonl',poster=post,gsc_submit=gsc)
    for kind, call in (('indexnow','indexnow_post'),('gsc','gsc_submit')):
        at=events.index(('replace',kind+'-intent.json'))
        assert events[at+1]==('dirsync','dep-1'), events
        assert events.index((call,None)) > at+1
        child=events.index(('replace',kind+'.json'))
        assert events[child+1]==('dirsync','dep-1')
    first_post=events.index(('indexnow_post',None))
    for parent in ('receipts','nested','dep-1'):
        assert ('dirsync',parent) in events[:first_post], (parent, events)


def test_crash_after_intent_or_post_never_replays(tmp_path, monkeypatch):
    """CR-02 fault injection: a crash between the durable intent and the provider
    response (or during GSC after IndexNow completed) refuses a blind re-POST on
    restart; only exact readback may resolve it."""
    old, new, pointer = _fixture_release(tmp_path)
    monkeypatch.setattr(notify,'current_pointer',lambda:pointer)
    monkeypatch.setattr(notify,'live_sitemap',lambda:new.joinpath('public/sitemap.xml').read_bytes())
    calls=[]
    class Crash(BaseException): pass
    def crashing_post(endpoint,payload): calls.append('indexnow'); raise Crash()
    def gsc(xml): calls.append('gsc'); return {'http_status':204}
    report=tmp_path/'r'; kw=dict(root=tmp_path,report_root=report,ledger=tmp_path/'l.jsonl',gsc_submit=gsc)
    with pytest.raises(Crash):
        notify.submit_deployment(pointer,old,poster=crashing_post,**kw)
    assert (report/'dep-1/indexnow-intent.json').exists() and not (report/'dep-1/indexnow.json').exists()
    def ok_post(endpoint,payload): calls.append('indexnow'); return 200,b''
    with pytest.raises(RuntimeError,match='uncertain_indexnow_requires_exact_readback'):
        notify.submit_deployment(pointer,old,poster=ok_post,**kw)
    assert calls==['indexnow']
    # Provider completed, crash during GSC: IndexNow is not replayed, GSC refuses.
    report2=tmp_path/'r2'; kw2={**kw,'report_root':report2}
    def crashing_gsc(xml): calls.append('gsc'); raise Crash()
    with pytest.raises(Crash):
        notify.submit_deployment(pointer,old,poster=ok_post,**{**kw2,'gsc_submit':crashing_gsc})
    assert (report2/'dep-1/indexnow.json').exists() and (report2/'dep-1/gsc-intent.json').exists()
    with pytest.raises(RuntimeError,match='uncertain_gsc_requires_exact_readback'):
        notify.submit_deployment(pointer,old,poster=ok_post,**kw2)
    assert calls==['indexnow','indexnow','gsc']
    assert not (tmp_path/'l.jsonl').exists()


def test_release_changed_set_deployment_ledger_and_replay(tmp_path, monkeypatch):
    import glp_release_main as release
    old, new = tmp_path/'old', tmp_path/'new'
    for root in (old,new):
        (root/'public/guides/unchanged').mkdir(parents=True)
        (root/'public/guides/unchanged/index.html').write_text('<html>same</html>')
        (root/'public/robots.txt').write_text('User-agent: *\nAllow: /\n')
    (new/'public/guides/new').mkdir()
    (new/'public/guides/new/index.html').write_text('<html>new guide</html>')
    (new/'public/sitemap.xml').write_text('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://glpclinicscout.com/guides/new/</loc></url><url><loc>https://glpclinicscout.com/guides/unchanged/</loc></url></urlset>')
    (new/'public/fixture.txt').write_text('fixture')
    (new/'data').mkdir(); (new/'data/indexnow.json').write_text(json.dumps({'host':'glpclinicscout.com','key':'fixture','key_location':'https://glpclinicscout.com/fixture.txt'}))
    final=tmp_path/'final.json'; final.write_text(json.dumps({'status':'PASS_PRODUCTION_TERMINAL','deployment':{'deployment_id':'dep-1'},'site_head':'head-1'}))
    pointer={'deployment_id':'dep-1','site_head':'head-1','release_root':str(new),'final_receipt':{'path':str(final),'sha256':hashlib.sha256(final.read_bytes()).hexdigest()}}
    pointer['supersedes'] = {'release_root': str(old)}
    calls=[]
    monkeypatch.setattr(notify,'current_pointer',lambda:pointer)
    monkeypatch.setattr(notify,'live_sitemap',lambda:new.joinpath('public/sitemap.xml').read_bytes())
    def post(endpoint,payload):
        calls.append(('indexnow',payload['urlList'])); return 200,b''
    def gsc(xml):
        calls.append(('gsc',xml));return {'http_status':204,'provider_readback':[{'path':notify.SITEMAP,'isPending':True}]}
    report=tmp_path/'receipts'; ledger=tmp_path/'indexnow.jsonl'
    kwargs=dict(root=tmp_path,report_root=report,ledger=ledger,poster=post,gsc_submit=gsc)
    with pytest.raises(RuntimeError, match='predecessor_binding'):
        notify.submit_deployment(pointer, new, **kwargs)
    assert calls == []
    value=notify.submit_deployment(pointer,old,**kwargs)
    assert value['status']=='PASS_SEARCH_SUBMITTED'
    assert value['deployment_id']=='dep-1'
    assert value['indexnow']['submitted_urls']==['https://glpclinicscout.com/guides/new/']
    rows=[json.loads(x) for x in ledger.read_text().splitlines()]
    assert len(rows)==1 and rows[0]['deployment_id']=='dep-1'
    assert rows[0]['receipt_sha256']==notify.sha(report/'dep-1/indexnow.json')
    assert notify.submit_deployment(pointer,old,**kwargs)==value
    assert len(calls)==2
    # An uncertain effect intent must not be retried as a fresh POST.
    (report/'dep-1/search-receipt.json').unlink(); (report/'dep-1/gsc.json').unlink()
    with pytest.raises(RuntimeError,match='uncertain_gsc'):
        notify.submit_deployment(pointer,old,**kwargs)
    assert len(calls)==2
    pointer['deployment_id']='different'
    with pytest.raises(RuntimeError,match='terminal_deployment_binding'):
        notify.submit_deployment(pointer,old,**kwargs)
    # Installed main publisher's terminal path must invoke this maintained seam.
    source=(ROOT/'scripts/glp_release_main.py').read_text()
    assert 'submit_deployment(' in source and 'search_submission' in source
