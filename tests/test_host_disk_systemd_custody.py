"""Loaded system-manager resolution in owned fixtures; no live unit queries."""
import importlib.util
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/host_disk_tmp_custody.py'
spec = importlib.util.spec_from_file_location('systemd_custody_candidate', SOURCE)
custody = importlib.util.module_from_spec(spec)
spec.loader.exec_module(custody)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    generated = tmp_path / 'run/systemd/generator'
    vendor = tmp_path / 'usr/lib/systemd/system'
    dependency = generated / 'multi-user.target.wants/systemd-networkd.service'
    dependency.parent.mkdir(parents=True)
    vendor.mkdir(parents=True)
    dependency.symlink_to('../systemd-networkd.service')
    fragment = vendor / dependency.name
    fragment.write_text('[Service]\nExecStart=/usr/bin/true\n')
    candidate = tmp_path / 'tmp/aeo-declared-Ab09xY'
    candidate.mkdir(parents=True)
    payload = candidate / 'payload.txt'
    payload.write_text('private disposable fixture\n')
    state = SimpleNamespace(
        generated=generated, vendor=vendor, dependency=dependency,
        fragment=fragment, candidate=candidate, payload=payload, calls=[],
        properties={'Id': dependency.name, 'LoadState': 'loaded',
                    'FragmentPath': str(fragment), 'SourcePath': '',
                    'DropInPaths': '', 'NeedDaemonReload': 'no'},
        unit_path=f'{generated} {vendor}', returncode=0, stderr='',
        transform=lambda raw: raw)

    def run(argv, **kwargs):
        state.calls.append((argv, kwargs))
        assert argv[:4] == ['/usr/bin/systemctl', '--system', '--no-pager', 'show']
        assert kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
                                 'SYSTEMD_COLORS': '0'}
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert 0 < kwargs['timeout'] <= 5
        if '--property=UnitPath' in argv:
            raw = 'UnitPath=' + state.unit_path + '\n'
        else:
            assert argv[-2:] == ['--', dependency.name]
            raw = ''.join(f'{key}={value}\n' for key, value in state.properties.items())
        return SimpleNamespace(returncode=state.returncode,
                               stdout=state.transform(raw), stderr=state.stderr)

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(custody, 'SYSTEMD_GENERATOR_ROOTS', (generated,), raising=False)
    monkeypatch.setattr(custody, 'SYSTEMD_ROOTS', (generated, vendor))
    monkeypatch.setattr(custody, 'SCHEDULE_ROOTS', (generated,))
    monkeypatch.setattr(custody, 'SYMLINK_ROOTS', ())
    monkeypatch.setattr(custody, 'REQUIRED_FILES', ())
    return state


def scan(manager, inodes=()):
    return custody._reference_scan(manager.candidate, time.monotonic() + 30, set(inodes))


def test_generated_dependency_uses_loaded_system_fragment(manager):
    identities = scan(manager)
    assert identities[manager.dependency] == custody._identity(manager.dependency)
    assert identities[manager.generated / manager.dependency.name] is None
    assert identities[manager.fragment] == custody._identity(manager.fragment)
    assert manager.calls
    custody._validate_references(identities, time.monotonic() + 30)
    assert manager.payload.read_text() == 'private disposable fixture\n'


@pytest.mark.parametrize('field', ['FragmentPath', 'SourcePath', 'DropInPaths'])
def test_actual_loaded_sources_keep_live_reference(manager, field):
    selected = manager.fragment if field == 'FragmentPath' else manager.vendor.parent / 'external.conf'
    selected.write_text('[Service]\nWorkingDirectory=' + str(manager.candidate) + '\n')
    manager.properties[field] = str(selected)
    with pytest.raises(ValueError, match='cron/systemd tmp custody'):
        scan(manager)
    assert manager.payload.is_file()


def test_source_and_dropins_outside_walk_are_read_and_bound(manager):
    source = manager.vendor.parent / 'init-script'
    dropin = manager.vendor.parent / 'override.conf'
    source.write_text('exec /usr/bin/true\n')
    dropin.write_text('[Service]\nRestart=no\n')
    manager.properties.update(SourcePath=str(source), DropInPaths=str(dropin))
    identities = scan(manager)
    assert identities[source] == custody._identity(source)
    assert identities[dropin] == custody._identity(dropin)


@pytest.mark.parametrize('reply', [
    '', 'UnitPath=/run/systemd/generator\nUnitPath=/usr/lib/systemd/system\n',
    'Unexpected=value\n', 'UnitPath=/run/systemd/generator\\x20bad\n',
    'UnitPath=relative/path\n', 'UnitPath=/run/systemd/generator\nextra text\n',
])
def test_malformed_manager_output_refuses(manager, reply):
    manager.transform = lambda raw: reply
    with pytest.raises(ValueError, match='systemd custody'):
        scan(manager)


@pytest.mark.parametrize('failure', ['exit', 'stderr', 'timeout', 'missing_binary', 'invalid_utf8'])
def test_failed_systemctl_refuses(manager, monkeypatch, failure):
    if failure == 'exit':
        manager.returncode = 1
    elif failure == 'stderr':
        manager.stderr = 'fixture manager warning'
    else:
        def failed(*args, **kwargs):
            if failure == 'timeout':
                raise subprocess.TimeoutExpired('fixture-systemctl', 5)
            if failure == 'invalid_utf8':
                raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'fixture invalid manager output')
            raise FileNotFoundError('fixture systemctl unavailable')
        monkeypatch.setattr(subprocess, 'run', failed)
    with pytest.raises(ValueError, match='systemd custody manager'):
        scan(manager)


@pytest.mark.parametrize('field,value', [
    ('Id', 'other.service'), ('LoadState', 'not-found'), ('NeedDaemonReload', 'yes'),
    ('NeedDaemonReload', ''), ('FragmentPath', ''), ('SourcePath', '/missing/source'),
    ('DropInPaths', '/missing/override.conf'),
])
def test_unresolved_stale_or_missing_loaded_selection_refuses(manager, field, value):
    manager.properties[field] = value
    with pytest.raises(ValueError):
        scan(manager)


def test_duplicate_unit_property_refuses(manager):
    manager.transform = lambda raw: raw + ('Id=systemd-networkd.service\n' if raw.startswith('Id=') else '')
    with pytest.raises(ValueError, match='malformed systemd custody'):
        scan(manager)


def test_missing_unit_property_refuses(manager):
    del manager.properties['SourcePath']
    with pytest.raises(ValueError, match='incomplete systemd custody'):
        scan(manager)


def test_generator_absent_from_actual_unit_path_refuses(manager):
    manager.unit_path = str(manager.vendor)
    with pytest.raises(ValueError, match='outside manager UnitPath'):
        scan(manager)


def test_fragment_outside_actual_unit_path_refuses(manager):
    manager.unit_path = str(manager.generated)
    with pytest.raises(ValueError, match='unsupported systemd custody selection'):
        scan(manager)


@pytest.mark.parametrize('kind', ['external', 'user', 'not_dependency', 'different_name'])
def test_other_dangling_links_keep_original_refusal(manager, monkeypatch, kind):
    manager.dependency.unlink()
    if kind == 'external':
        manager.dependency.symlink_to(manager.generated.parent / manager.dependency.name)
    elif kind == 'different_name':
        manager.dependency.symlink_to('../other.service')
    elif kind == 'user':
        user_root = manager.generated.parent / 'user-generator'
        target = user_root / 'multi-user.target.wants' / manager.dependency.name
        target.parent.mkdir(parents=True)
        target.symlink_to('../' + manager.dependency.name)
        monkeypatch.setattr(custody, 'SCHEDULE_ROOTS', (user_root,))
    else:
        (manager.generated / 'unknown-directory').mkdir()
        (manager.generated / 'unknown-directory' / manager.dependency.name).symlink_to('../' + manager.dependency.name)
    with pytest.raises(ValueError, match='missing tmp custody source'):
        scan(manager)
    assert manager.calls == []


def test_existing_dependency_target_uses_ordinary_text_guard(manager):
    target = manager.generated / manager.dependency.name
    target.write_text('[Service]\nWorkingDirectory=' + str(manager.candidate))
    with pytest.raises(ValueError, match='cron/systemd tmp custody'):
        scan(manager)
    assert manager.calls == []


def test_selected_fragment_alias_refuses(manager):
    alternate = manager.fragment.with_suffix('.original')
    manager.fragment.rename(alternate)
    manager.fragment.symlink_to(alternate)
    with pytest.raises(ValueError, match='aliased or invalid systemd custody'):
        scan(manager)


def test_unreadable_selected_source_refuses(manager, monkeypatch):
    original = Path.read_text
    def denied(path, *args, **kwargs):
        if path == manager.fragment:
            raise PermissionError('fixture unreadable loaded fragment')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', denied)
    with pytest.raises(PermissionError, match='unreadable loaded fragment'):
        scan(manager)


def test_selected_fragment_inode_hold_refuses(manager):
    identity = custody._identity(manager.fragment)
    with pytest.raises(ValueError, match='loaded systemd tmp custody'):
        scan(manager, [identity[:2]])


def test_selected_source_within_candidate_refuses_without_text_reference(manager):
    manager.properties['SourcePath'] = str(manager.payload)
    with pytest.raises(ValueError, match='loaded systemd tmp custody'):
        scan(manager)


@pytest.mark.parametrize('change', ['selection', 'unit_path', 'reload', 'lexical', 'link', 'dropin', 'alias'])
def test_final_custody_validation_refuses_manager_or_path_drift(manager, change):
    dropin = manager.vendor.parent / 'override.conf'
    dropin.write_text('[Service]\nRestart=no\n')
    manager.properties['DropInPaths'] = str(dropin)
    identities = scan(manager)
    if change == 'selection':
        manager.properties['SourcePath'] = str(dropin)
    elif change == 'unit_path':
        manager.unit_path += ' /unused-new-search-root'
    elif change == 'reload':
        manager.properties['NeedDaemonReload'] = 'yes'
    elif change == 'lexical':
        (manager.generated / manager.dependency.name).write_text('[Service]\nExecStart=/usr/bin/true')
    elif change == 'link':
        manager.dependency.unlink()
        manager.dependency.symlink_to('../other.service')
    elif change == 'dropin':
        dropin.write_text('[Service]\nWorkingDirectory=' + str(manager.candidate))
    else:
        alternate = manager.vendor.with_name('moved-units')
        manager.vendor.rename(alternate)
        manager.vendor.symlink_to(alternate, target_is_directory=True)
    with pytest.raises(ValueError):
        custody._validate_references(identities, time.monotonic() + 30)
    assert manager.payload.is_file()


def test_selection_change_during_scan_refuses(manager):
    def changed(raw):
        if len(manager.calls) > 2:
            return raw.replace('NeedDaemonReload=no', 'NeedDaemonReload=yes')
        return raw
    manager.transform = changed
    with pytest.raises(ValueError, match='unresolved or stale systemd custody'):
        scan(manager)
