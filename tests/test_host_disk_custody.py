from pathlib import Path
import time
import host_disk_custody as custody
import host_disk_cold_classes as cold


def test_fresh_scheduler_skill_lane_pins(tmp_path):
    bus = tmp_path / 'business'
    home = tmp_path / 'hermes'; home.mkdir()
    report = bus / 'reports/ops/old-20260901'; report.mkdir(parents=True)
    skill = home / 'skills/example'; skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(str(report / 'proof.txt'))
    units = tmp_path / 'units'; units.mkdir()
    unit = units / 'future.service'
    unit.write_text('[Service]\nWorkingDirectory=' + str(report) + '\n')
    lanes = bus / 'reports/ops/fable-plan-2-20261009/lanes'
    for name in ('Q4', 'Q11', 'D1', 'P10', 'P22', 'Q3', 'P9'):
        (lanes / name).mkdir(parents=True)
    pins = custody.collect(bus=bus, home=home, unit_roots=(units,), cron_text='')
    assert {report, report/'proof.txt', bus/'data'} <= pins
    assert {lanes/n for n in ('Q4','Q11','D1','P10','P22')} <= pins
    assert lanes/'Q3' not in pins and lanes/'P9' not in pins
    unit.write_text('[Service]\nWorkingDirectory=' + str(report/'new') + '\n')
    fresh = custody.collect(bus=bus, home=home, unit_roots=(units,), cron_text='')
    assert report/'new' in fresh and report not in fresh


def test_archive_honors_future_scheduler_reference(tmp_path, monkeypatch):
    home = tmp_path/'hermes'; home.mkdir()
    bus = tmp_path/'business'
    root = bus/'reports/ops/old-20260901'; root.mkdir(parents=True)
    proof = root/'proof'; proof.write_bytes(b'proof')
    real_collect = custody.collect
    monkeypatch.setattr(custody, 'collect', lambda: real_collect(
        bus=bus, home=home, unit_roots=(), cron_text='cd '+str(root)+' && true'))
    now = time.time(); monkeypatch.setattr(cold.time, 'time', lambda: now+10*86400)
    class API:
        def path_is_active_privileged(self, p): return False
    row = cold.cold_archive(API(), root, tmp_path/'out/a.tar.zst',
                            dry_run=False, deadline=time.monotonic()+60)
    assert row['status']=='REFUSED' and proof.exists()


def test_consumer_alias_target_is_refreshed(tmp_path,monkeypatch):
    first=tmp_path/'first';first.mkdir()
    second=tmp_path/'second';second.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(first)
    monkeypatch.setattr(custody,'collect',lambda:{alias})
    monkeypatch.setattr(cold,'live_symlink_targets',lambda:set())
    assert first in cold.custody_pins(())
    alias.unlink();alias.symlink_to(second)
    pins=cold.custody_pins(())
    assert second in pins and first not in pins


def test_reports_wide_rule_keeps_recovery_root_and_named_pins(tmp_path,monkeypatch):
    import host_disk_large_sweep as sweep
    bus=tmp_path/'business'
    base=bus/'reports'
    for name in ('ops/old-worker','glp-clinic-scout/evidence','old-build','host-disk-prune','live'):
        (base/name).mkdir(parents=True)
    monkeypatch.setattr(sweep,'BUS',bus)
    monkeypatch.setattr(custody,'collect',lambda:set())
    roots=sweep.cold_report_roots((base/'live',),pressure=True)
    assert set(roots)=={base/'ops/old-worker',base/'glp-clinic-scout/evidence',base/'old-build'}
    assert sweep.cold_report_roots((),pressure=False)==[]
