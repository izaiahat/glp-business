"""Maintenance lifecycle contracts; all schedulers and locks are disposable fixtures."""
from __future__ import annotations

import copy
import fcntl
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location(
        "maintenance_window_under_test", ROOT / "scripts" / "maintenance_window.py")
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


class SimulatedPowerLoss(BaseException):
    """Bypass ordinary exception rollback exactly at an uncertain mutation."""


class FakeBackend:
    """Durable scheduler state survives a new MaintenanceWindow instance."""

    def __init__(self):
        self.entries = [
            {"kind": "hermes", "id": "enabled", "command": "python affected.py",
             "affected": True, "trace": ["affected.py"]},
            {"kind": "hermes", "id": "already-paused", "command": "python affected.py",
             "affected": True, "trace": ["affected.py"]},
            {"kind": "systemd", "id": "timer", "command": "python affected.py",
             "affected": True, "trace": ["affected.py"]},
            {"kind": "crontab", "id": "line", "command": "python affected.py",
             "affected": True, "trace": ["affected.py"]},
        ]
        self.states = {
            "enabled": {"paused": False, "enabled": True, "schedule": "0 * * * *"},
            "already-paused": {"paused": True, "enabled": False, "schedule": "17 2 * * *"},
            "timer": {"paused": False, "active": True, "enabled": "enabled-runtime"},
            "line": {"paused": False, "line": "7 * * * * python affected.py # retained"},
        }
        self.original = copy.deepcopy(self.states)
        self.calls = []
        self.crash_after_pause = None
        self.fail_pause = None
        self.fail_restore = None
        self.crash_after_restore = None
        self.running = []

    def inventory(self):
        return {"entries": copy.deepcopy(self.entries), "complete": True, "errors": []}

    def snapshot(self, entry):
        self.calls.append(("snapshot", entry["id"]))
        return copy.deepcopy(self.states[entry["id"]])

    def pause(self, entry, prior):
        identity = entry["id"]
        self.calls.append(("pause", identity))
        self.states[identity]["paused"] = True
        if self.crash_after_pause == identity:
            self.crash_after_pause = None
            raise SimulatedPowerLoss(identity)
        if self.fail_pause == identity:
            raise RuntimeError("injected_pause_failure")

    def restore(self, entry, prior):
        identity = entry["id"]
        self.calls.append(("restore", identity))
        if self.fail_restore == identity:
            raise RuntimeError("injected_restore_failure")
        self.states[identity] = copy.deepcopy(prior)
        if self.crash_after_restore == identity:
            self.crash_after_restore = None
            raise SimulatedPowerLoss(identity)

    def paused(self, entry, prior):
        return self.states[entry["id"]]["paused"]

    def inflight(self, entries):
        return copy.deepcopy(self.running)


def new_window(module, tmp_path, backend, **kwargs):
    return module.MaintenanceWindow(
        backend, tmp_path / "window.json", [tmp_path / "reader.lock", tmp_path / "writer.lock"],
        drain_timeout=kwargs.pop("drain_timeout", 0.05), poll_interval=0.001, **kwargs)


def assert_locks_released(tmp_path):
    for name in ("reader.lock", "writer.lock"):
        with (tmp_path / name).open("a+") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)


def assert_locks_held(tmp_path):
    for name in ("reader.lock", "writer.lock"):
        with (tmp_path / name).open("a+") as fd:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_enter_exit_restores_exact_mixed_prior_states(module, tmp_path):
    backend = FakeBackend()
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        assert all(state["paused"] for state in backend.states.values())
        assert_locks_held(tmp_path)
        assert window.status()["state"] == "active"
        assert window.status()["verified"] is True
        window.exit()
        assert backend.states == backend.original
        assert window.status()["state"] == "closed"
        assert_locks_released(tmp_path)
    finally:
        window.release_locks()


def test_enter_and_exit_are_idempotent_without_replacing_preimages(module, tmp_path):
    backend = FakeBackend()
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        snapshots = [call for call in backend.calls if call[0] == "snapshot"]
        window.enter()
        assert [call for call in backend.calls if call[0] == "snapshot"] == snapshots
        window.exit()
        restores = [call for call in backend.calls if call[0] == "restore"]
        window.exit()
        assert [call for call in backend.calls if call[0] == "restore"] == restores
        assert backend.states == backend.original
    finally:
        window.release_locks()


@pytest.mark.parametrize("recover", ["enter", "exit"])
def test_crash_after_pause_before_receipt_preserves_prior_state(module, tmp_path, recover):
    backend = FakeBackend()
    backend.crash_after_pause = "enabled"
    first = new_window(module, tmp_path, backend)
    try:
        with pytest.raises(SimulatedPowerLoss):
            first.enter()
    finally:
        first.release_locks()  # A real process death closes its flock descriptors.
    assert backend.states["enabled"]["paused"] is True
    durable = json.loads((tmp_path / "window.json").read_text())
    record = next(row for row in durable["entries"] if row["entry"]["id"] == "enabled")
    assert record["prior"] == backend.original["enabled"]
    assert record["intent"]
    resumed = new_window(module, tmp_path, backend)
    try:
        getattr(resumed, recover)()
        if recover == "enter":
            assert resumed.status()["verified"] is True
            resumed.exit()
        assert backend.states == backend.original
        assert_locks_released(tmp_path)
    finally:
        resumed.release_locks()


def test_drain_timeout_aborts_and_restores_without_killing_work(module, tmp_path):
    backend = FakeBackend()
    backend.running = [{"pid": 98765, "start_ticks": "100", "command": "python affected.py"}]
    window = new_window(module, tmp_path, backend, drain_timeout=0)
    with pytest.raises(module.WindowError, match="drain"):
        window.enter()
    assert backend.states == backend.original
    assert backend.running[0]["pid"] == 98765
    assert window.status()["state"] == "closed"
    durable = json.loads((tmp_path / "window.json").read_text())
    assert durable['drain']['timeout_seconds'] == 0
    assert durable['drain']['blockers'] == backend.running
    assert durable['drain']['last_observed_at'] >= durable['drain']['started_at']
    assert_locks_released(tmp_path)


def test_drain_census_retains_last_blocker_not_the_finished_first_holder(module, tmp_path):
    backend = FakeBackend()
    first = [{'kind': 'hermes', 'id': 'finished-job', 'reason': 'native_execution_owner_live'}]
    final = [{'kind': 'process', 'pid': 98765, 'start_ticks': 123,
              'reason': 'affected_task_tree', 'tasks': ['glp_host_ingress.py'], 'sources': []}]
    clock = [0]
    def census(entries):
        return first if clock[0] == 0 else final
    backend.inflight = census
    def advance(seconds):
        clock[0] += seconds
    window = new_window(module, tmp_path, backend, drain_timeout=0.002,
                        clock=lambda: clock[0], sleeper=advance)
    with pytest.raises(module.WindowError, match='drain_timeout'):
        window.enter()
    durable = json.loads(window.journal.read_text())
    assert durable['drain']['blockers'] == final
    assert durable['state'] == 'closed'
    assert backend.states == backend.original
    assert_locks_released(tmp_path)


def test_uncertain_pause_failure_restores_entry_that_may_have_changed(module, tmp_path):
    backend = FakeBackend()
    backend.fail_pause = "timer"
    window = new_window(module, tmp_path, backend)
    with pytest.raises(RuntimeError, match="pause"):
        window.enter()
    assert backend.states == backend.original
    assert window.status()["state"] == "closed"
    assert_locks_released(tmp_path)


def test_failed_restore_stays_recoverable_and_retry_finishes(module, tmp_path):
    backend = FakeBackend()
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        backend.fail_restore = "enabled"
        with pytest.raises(module.WindowError, match="restor"):
            window.exit()
        assert window.status()["state"] != "closed"
        assert backend.states["enabled"]["paused"]
        backend.fail_restore = None
        window.exit()
        assert backend.states == backend.original
        assert window.status()["state"] == "closed"
        assert_locks_released(tmp_path)
    finally:
        window.release_locks()


def test_active_journal_without_lock_owner_is_not_verified(module, tmp_path):
    backend = FakeBackend()
    window = new_window(module, tmp_path, backend)
    window.enter()
    window.release_locks()
    assert window.status()["verified"] is False
    window.exit()
    assert backend.states == backend.original


def test_active_verification_requires_every_configured_lock(module, tmp_path):
    backend = FakeBackend()
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        data = module.read_journal(window.journal)
        data["locks"] = data["locks"][:1]
        module.atomic_json(window.journal, data)
        assert window.status()["verified"] is False
        window.exit()
    finally:
        window.release_locks()


def test_lock_contention_aborts_and_restores(module, tmp_path):
    backend = FakeBackend()
    window = new_window(module, tmp_path, backend)
    with (tmp_path / "writer.lock").open("a+") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((module.WindowError, BlockingIOError)):
            window.enter()
    assert backend.states == backend.original
    assert_locks_released(tmp_path)


def test_incomplete_inventory_never_pauses_scheduler(module, tmp_path):
    backend = FakeBackend()
    backend.inventory = lambda: {
        "entries": copy.deepcopy(backend.entries), "complete": False,
        "errors": ["unclassifiable entry: opaque shell command"],
    }
    window = new_window(module, tmp_path, backend)
    with pytest.raises(module.WindowError):
        window.enter()
    assert backend.calls == []
    assert backend.states == backend.original


def test_scheduler_reenabled_during_window_invalidates_verification(module, tmp_path):
    backend = FakeBackend()
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        backend.states["enabled"]["paused"] = False
        assert window.status()["verified"] is False
        window.exit()
        assert backend.states == backend.original
    finally:
        window.release_locks()


def test_crash_after_restore_before_receipt_can_finish_rollback(module, tmp_path):
    backend = FakeBackend()
    first = new_window(module, tmp_path, backend)
    first.enter()
    backend.crash_after_restore = "already-paused"
    try:
        with pytest.raises(SimulatedPowerLoss):
            first.exit()
    finally:
        first.release_locks()
    resumed = new_window(module, tmp_path, backend)
    try:
        resumed.exit()
        assert backend.states == backend.original
        assert resumed.status()["state"] == "closed"
        assert_locks_released(tmp_path)
    finally:
        resumed.release_locks()


def test_unaffected_entry_is_never_paused_or_restored(module, tmp_path):
    backend = FakeBackend()
    backend.entries.append({"kind": "hermes", "id": "unrelated", "command": "/usr/bin/true",
                            "affected": False, "trace": []})
    backend.states["unrelated"] = {"paused": False, "enabled": True}
    original = copy.deepcopy(backend.states)
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        assert backend.states["unrelated"] == original["unrelated"]
        window.exit()
        assert backend.states == original
        assert not any(identity == "unrelated" for _, identity in backend.calls)
    finally:
        window.release_locks()


def crontab_backend(module, tmp_path):
    class Backend(module.LinuxBackend):
        def __init__(self):
            super().__init__(tmp_path / "root", tmp_path / "hermes", tmp_path / "window.json")
            self.original = "# retained header\r\n7 * * * * python first.py\r\n19 * * * * python second.py\n"
            self.text = self.original
            self.writes = []
            self.crash_on_write = None

        def inventory(self):
            return {"complete": True, "errors": [], "entries": [
                {"kind": "crontab", "id": str(index), "command": "python " + name,
                 "affected": True, "trace": [], "prior": {"index": index, "line": line}}
                for index, name, line in (
                    (1, "first.py", "7 * * * * python first.py"),
                    (2, "second.py", "19 * * * * python second.py"),
                )]}

        def command(self, argv, payload=None):
            if argv == ["/usr/bin/crontab", "-l"]:
                return self.text
            assert argv == ["/usr/bin/crontab", "-"]
            assert payload is not None
            self.text = payload
            self.writes.append(payload)
            if self.crash_on_write == len(self.writes):
                self.crash_on_write = None
                raise SimulatedPowerLoss("crontab write applied, receipt lost")
            return ""

        def inflight(self, entries):
            return []

    return Backend()


def test_native_crontab_multiple_lines_preserves_complete_exact_bytes(module, tmp_path):
    backend = crontab_backend(module, tmp_path)
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        assert backend.text.count("# GLP44/73 maintenance ") == 2
        assert window.status()["verified"]
        window.exit()
        assert backend.text == backend.original
    finally:
        window.release_locks()


@pytest.mark.parametrize("crash_write", [1, 3])
def test_native_crontab_uncertain_pause_and_restore_writes_recover(module, tmp_path, crash_write):
    backend = crontab_backend(module, tmp_path)
    backend.crash_on_write = crash_write
    first = new_window(module, tmp_path, backend)
    try:
        with pytest.raises(SimulatedPowerLoss):
            first.enter()
            first.exit()
    finally:
        first.release_locks()
    resumed = new_window(module, tmp_path, backend)
    resumed.exit()
    assert backend.text == backend.original


def test_native_crontab_concurrent_edit_is_preserved_and_requires_recovery(module, tmp_path):
    backend = crontab_backend(module, tmp_path)
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        backend.text += "# unrelated new operator note\n"
        changed = backend.text
        with pytest.raises(module.WindowError, match="restore"):
            window.exit()
        assert backend.text == changed
        assert window.status()["recovery_required"]
    finally:
        window.release_locks()


def test_systemd_snapshot_refresh_has_bounded_census_and_equivalent_metadata(module, tmp_path, monkeypatch):
    """Real discovery/parsing/hashing; only scheduler command transport is fake."""
    import maintenance_inventory as inventory

    root, hermes = tmp_path / "root", tmp_path / "hermes"
    (root / "scripts").mkdir(parents=True)
    (root / "ops/n8n-bridge").mkdir(parents=True)
    (hermes / "cron").mkdir(parents=True)
    source = root / "scripts/glp_price_index_weekly.py"
    source.write_text('print("fixture")\n')
    (hermes / "cron/jobs.json").write_text('{"jobs": []}')
    (root / "ops/n8n-bridge/actions.json").write_text('{}')
    command = "python3 " + str(source)
    calls, units, scans = [], {}, []
    discover = inventory.discover

    def counted_discover(*args, **kwargs):
        scans.append(1)
        return discover(*args, **kwargs)

    monkeypatch.setattr(inventory, "discover", counted_discover)

    def probe(argv, payload=None):
        assert payload is None
        calls.append(argv)
        if argv == ["/usr/bin/crontab", "-l"]:
            return ""
        assert argv[:2] == ["/usr/bin/systemctl", "--user"]
        operation = argv[2]
        if operation in {"list-unit-files", "list-units"}:
            return "\n".join(name + " enabled" for name in units if name.endswith(".timer"))
        assert operation == "show", "regression must never mutate schedulers"
        assert "Environment" not in argv[-1].split("=", 1)[-1].split(",")
        return "\n".join(key + "=" + value for key, value in units[argv[3]].items())

    for count in (1, 8):
        units.clear()
        units["n8n-bridge.service"] = {
            "Id": "n8n-bridge.service", "LoadState": "loaded", "ActiveState": "active",
            "UnitFileState": "enabled", "MainPID": "123", "ExecStart": "{ argv[]=/bin/true ; }"}
        for index in range(count):
            name, service = f"reader{index}.timer", f"reader{index}.service"
            units[name] = {
                "Id": name, "LoadState": "loaded", "ActiveState": "active" if index % 2 == 0 else "inactive",
                "UnitFileState": "enabled-runtime" if index % 2 == 0 else "disabled", "Triggers": service,
                "TimersCalendar": "{ OnCalendar=daily ; next_elapse=100 ; }", "Persistent": "yes"}
            units[service] = {
                "Id": service, "LoadState": "loaded", "ActiveState": "inactive", "UnitFileState": "static",
                "ExecStart": "{ argv[]=" + command + " ; ignore_errors=no ; pid=3 ; status=0 ; }",
                "ExecStartPre": "{ argv[]=" + command + " ; ignore_errors=no ; pid=4 ; }",
                "WorkingDirectory": str(root), "EnvironmentFiles": "/not-opened.env", "Restart": "no"}
        backend = module.LinuxBackend(root, hermes, tmp_path / "window.json")
        monkeypatch.setattr(backend, "command", probe)
        window = new_window(module, tmp_path, backend)
        scans.clear()
        report = window.plan()
        rows = [row for row in report["entries"] if row["kind"] == "systemd"]
        assert len(rows) == count
        assert set(report["coverage"]) == {"hermes", "crontab", "systemd", "n8n"}
        assert all(row["affected"] and row["source_sha256"] for row in rows)
        calls.clear()
        snapshots = [backend.snapshot(row) for row in rows]
        for row, prior in zip(rows, snapshots):
            backend.timer_unchanged(row, prior, refresh=True)
        assert len(scans) == 1, f"{count} timer snapshots/refreshes repeated full inventory {len(scans)} times"
        assert len(calls) == 4 * count  # Fresh timer + service show per snapshot and refresh.
        assert all(argv[2] == "show" for argv in calls)
        assert snapshots == [row["prior"] for row in rows]
        # Compare freshly parsed production metadata with full discovery, not a fabricated prior.
        for row in rows:
            metadata, _ = inventory.timer_metadata(row["id"], root, probe=lambda argv: probe(["/usr/bin/" + argv[0], *argv[1:]]))
            assert metadata == {key: row[key] for key in ("command", "prior", "cwd")}
        assert backend._report is report  # Target refresh must not replace the full-cohort oracle.
        first = rows[0]
        timer, service = units[first["id"]], units[first["prior"]["service"]]
        for target, field, value in (
            (timer, "UnitFileState", "enabled"), (timer, "Persistent", "no"),
            (timer, "Triggers", "reader1.service" if count > 1 else "n8n-bridge.service"),
            (service, "UnitFileState", "enabled"), (service, "ExecStart", "{ argv[]=/bin/false ; }"),
            (service, "ExecStartPre", "{ argv[]=/bin/false ; }"), (service, "WorkingDirectory", "/changed"),
        ):
            original = target[field]
            target[field] = value
            with pytest.raises(module.WindowError, match="timer_(command|configuration)_changed"):
                backend.timer_unchanged(first, first["prior"], refresh=True)
            target[field] = original
        assert len(scans) == 1
        fresh = window.plan()
        assert len(scans) == 2  # Cohort boundaries still require complete fresh discovery.
        assert fresh == report


def systemd_backend(module, tmp_path):
    class Backend(module.LinuxBackend):
        def __init__(self):
            super().__init__(tmp_path / "root", tmp_path / "hermes", tmp_path / "window.json")
            self.root.mkdir()
            self.units = {
                "active-disabled.timer": {"timer_active": "active", "timer_enabled": "disabled", "service": "one.service"},
                "active-runtime.timer": {"timer_active": "active", "timer_enabled": "enabled-runtime", "service": "two.service"},
                "inactive.timer": {"timer_active": "inactive", "timer_enabled": "disabled", "service": "three.service"},
            }
            self.original = copy.deepcopy(self.units)
            self.calls = []

        def inventory(self):
            from maintenance_inventory import timer_metadata
            self._report = {"complete": True, "errors": [], "entries": [
                {"kind": "systemd", "id": name, "affected": True, "trace": [],
                 **timer_metadata(name, self.root, probe=lambda argv: self.command(
                     ["/usr/bin/" + argv[0], *argv[1:]]))[0]}
                for name in self.units]}
            return self._report

        def command(self, argv, payload=None):
            assert argv[:2] == ["/usr/bin/systemctl", "--user"]
            operation, name = argv[2:4]
            if operation == "show":
                if argv[4:] == ["-p", "ActiveState", "--value"]:
                    return self.units[name]["timer_active"] + "\n"
                assert argv[4].startswith("--property=")
                if name in self.units:
                    unit = self.units[name]
                    return (f"LoadState=loaded\nActiveState={unit['timer_active']}\n"
                            f"UnitFileState={unit['timer_enabled']}\nTriggers={unit['service']}\n")
                assert name in {unit["service"] for unit in self.units.values()}
                return ("LoadState=loaded\nActiveState=inactive\nUnitFileState=static\n"
                        "ExecStart={ argv[]=python affected.py ; }\n")
            assert operation in {"stop", "start"}, "window must not change timer enablement"
            self.calls.append((operation, name))
            self.units[name]["timer_active"] = "active" if operation == "start" else "inactive"
            return ""

        def inflight(self, entries):
            return []

    return Backend()


def test_native_systemd_restores_active_state_without_enabling_disabled_timers(module, tmp_path):
    backend = systemd_backend(module, tmp_path)
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        assert all(unit["timer_active"] == "inactive" for unit in backend.units.values())
        window.exit()
        assert backend.units == backend.original
        assert ("start", "inactive.timer") not in backend.calls
        assert ("stop", "inactive.timer") not in backend.calls
    finally:
        window.release_locks()


def test_native_systemd_refuses_restore_after_operator_enablement_change(module, tmp_path):
    backend = systemd_backend(module, tmp_path)
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        backend.units["active-disabled.timer"]["timer_enabled"] = "enabled"
        with pytest.raises(module.WindowError, match="restore"):
            window.exit()
        assert backend.units["active-disabled.timer"]["timer_active"] == "inactive"
        assert ("start", "active-disabled.timer") not in backend.calls
    finally:
        window.release_locks()


@pytest.mark.parametrize("tamper", ["pid_start", "inode", "shared_lock", "released_lock"])
def test_linux_keeper_verification_requires_identity_inode_and_exclusive_lock(module, tmp_path, tamper):
    import os

    path = tmp_path / "held.lock"
    with path.open("a+") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = os.fstat(fd.fileno())
        evidence = {"keeper": module.identity(), "locks": [
            {"path": str(path), "fd": fd.fileno(), "device": st.st_dev, "inode": st.st_ino}]}
        assert module.locks_verified(evidence)
        if tamper == "pid_start":
            evidence["keeper"]["start_ticks"] += 1
        elif tamper == "inode":
            path.unlink()
            path.touch()
        elif tamper == "shared_lock":
            fcntl.flock(fd, fcntl.LOCK_SH)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        assert not module.locks_verified(evidence)


def test_adoption_guard_excludes_exit_for_entire_operation(module, tmp_path, monkeypatch):
    backend = FakeBackend()
    backend.root = tmp_path.resolve()
    backend.hermes = (tmp_path / "hermes").resolve()
    window = new_window(module, tmp_path, backend)
    monkeypatch.setattr(module, "LinuxBackend", lambda *_: backend)
    monkeypatch.setattr(module, "default_locks", lambda *_: window.lock_paths)
    try:
        window.enter()
        with module.require_active_window(window.journal, backend.root, backend.hermes):
            with pytest.raises(module.WindowError, match="window_operation_busy"):
                window.exit()
            assert window.status()["state"] == "active"
            assert all(state["paused"] for state in backend.states.values())
        window.exit()
        assert backend.states == backend.original
    finally:
        window.release_locks()


def hermes_backend(module, tmp_path):
    import threading

    class Backend(module.LinuxBackend):
        def __init__(self):
            super().__init__(tmp_path / "root", tmp_path / "hermes", tmp_path / "window.json")
            self.hermes.mkdir()
            self.jobs = {
                "enabled": {"id": "enabled", "enabled": True, "state": "scheduled",
                            "pending_slot": "2026-09-30T12:00:00Z", "next_run_at": "2026-09-30T12:00:00Z",
                            "schedule": {"kind": "cron", "expr": "0 * * * *"},
                            "script": "wrapper.py", "script_sha256": "a" * 64},
                "paused": {"id": "paused", "enabled": False, "state": "paused",
                           "paused_at": "2026-09-28T12:00:00Z", "paused_reason": "operator hold",
                           "schedule": {"kind": "cron", "expr": "1 * * * *"},
                           "script": "other.py", "script_sha256": "b" * 64},
            }
            self.original = copy.deepcopy(self.jobs)
            self.tool_calls = []
            self.crash_after_restore_save = False
            self._jobs_file_lock = threading.RLock()
            self.fire_depth = 0

        def inventory(self):
            return {"complete": True, "errors": [], "entries": [
                {"kind": "hermes", "id": jid, "command": "python " + job["script"],
                 "affected": True, "trace": []}
                for jid, job in self.jobs.items()]}

        def job(self, jid):
            return copy.deepcopy(self.jobs[jid])

        def scheduler(self):
            return self

        def _jobs_lock_file(self):
            return self.hermes / ".jobs.lock"

        @contextmanager
        def _fire_job_lock(self, jid):
            self.fire_depth += 1
            try:
                yield True
            finally:
                self.fire_depth -= 1

        @contextmanager
        def _jobs_lock(self):
            yield

        def _record_load_stamp(self, value):
            pass

        def load_jobs(self):
            return copy.deepcopy(list(self.jobs.values()))

        def _save_jobs_unlocked(self, jobs, *, removed_ids=None, replace=False):
            assert removed_ids is None and replace is False
            restoring = self.jobs["enabled"]["enabled"] is False and next(
                job for job in jobs if job["id"] == "enabled")["enabled"] is True
            if restoring:
                assert self.fire_depth, "restoration must hold the native fire fence"
            self.jobs = {job["id"]: copy.deepcopy(job) for job in jobs}
            if restoring and self.crash_after_restore_save:
                self.crash_after_restore_save = False
                raise SimulatedPowerLoss("native restore committed before receipt")

        def cron_tool(self, action, jid):
            self.tool_calls.append((action, jid))
            jobs = self.load_jobs()
            target = next(job for job in jobs if job["id"] == jid)
            if action == "pause":
                target.update(enabled=False, state="paused", paused_at="2026-10-01T01:00:00Z",
                              paused_reason="GLP44/73 maintenance; restore from " + str(self.journal))
                target.pop("pending_slot", None)
            else:
                assert action == "resume"
                assert self.fire_depth, "resume must be fenced until exact fields are repaired"
                target.update(enabled=True, state="scheduled", paused_at=None, paused_reason=None,
                              next_run_at="2026-10-01T02:00:00Z")
            # Native tools ultimately commit through the scheduler saver.
            self._save_jobs_unlocked(jobs, removed_ids=None, replace=False)

        def inflight(self, entries):
            return []

    return Backend()


def test_native_hermes_restores_exact_controls_and_preserves_adopted_pin(module, tmp_path):
    backend = hermes_backend(module, tmp_path)
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        assert backend.tool_calls == [("pause", "enabled")]
        active = module.ActiveWindow(window.journal, backend)
        before = backend.job("enabled")
        after = {**before, "script_sha256": "c" * 64}
        active.accept_job_update("enabled", before, after)
        backend.jobs["enabled"] = after
        window.exit()
        expected = copy.deepcopy(backend.original)
        expected["enabled"]["script_sha256"] = "c" * 64
        assert backend.jobs == expected
        assert backend.tool_calls == [("pause", "enabled")]
    finally:
        window.release_locks()


def test_native_hermes_prepared_pin_update_can_restore_after_adoption_rollback(module, tmp_path):
    backend = hermes_backend(module, tmp_path)
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        active = module.ActiveWindow(window.journal, backend)
        before = backend.job("enabled")
        active.accept_job_update("enabled", before, {**before, "script_sha256": "c" * 64})
        # Simulate failure before save (or adoption rollback to the old pin).
        window.exit()
        assert backend.jobs == backend.original
    finally:
        window.release_locks()


def test_native_hermes_crash_after_exact_restore_save_is_recoverable(module, tmp_path):
    backend = hermes_backend(module, tmp_path)
    first = new_window(module, tmp_path, backend)
    first.enter()
    backend.crash_after_restore_save = True
    try:
        with pytest.raises(SimulatedPowerLoss):
            first.exit()
    finally:
        first.release_locks()
    resumed = new_window(module, tmp_path, backend)
    resumed.exit()
    assert backend.jobs == backend.original
    assert resumed.status()["state"] == "closed"


@pytest.mark.parametrize("field,value", [
    ("script", "unexpected.py"),
    ("schedule", {"kind": "cron", "expr": "* * * * *"}),
    ("paused_reason", "new operator emergency hold"),
])
def test_native_hermes_refuses_to_resume_unreviewed_configuration(module, tmp_path, field, value):
    backend = hermes_backend(module, tmp_path)
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        backend.jobs["enabled"][field] = value
        with pytest.raises(module.WindowError, match="restore"):
            window.exit()
        assert backend.jobs["enabled"][field] == value
        assert backend.jobs["enabled"]["enabled"] is False
        assert ("resume", "enabled") not in backend.tool_calls
    finally:
        window.release_locks()


@pytest.mark.parametrize("scenario", ["exit", "crash-exit", "crash-resume", "expiry", "concurrent-enter"])
def test_detached_cli_keeper_owns_locks_and_recovers_after_real_process_crash(tmp_path, scenario):
    """Run actual CLI/keeper code with a file-backed fake scheduler only."""
    import os
    import signal
    import subprocess

    wrapper = tmp_path / "sandbox_keeper.py"
    wrapper.write_text(
        "import sys, json\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "import maintenance_window as mw\n"
        "class Backend:\n"
        " def __init__(self, root, hermes, journal):\n"
        "  self.root, self.hermes = Path(root).resolve(), Path(hermes).resolve()\n"
        "  self.file = self.root / 'scheduler.json'\n"
        " def inventory(self):\n"
        "  return {'complete': True, 'errors': [], 'entries': [{'kind': 'hermes', 'id': 'fixture', 'command': 'fixture', 'affected': True, 'trace': []}]}\n"
        " def snapshot(self, entry): return json.loads(self.file.read_text())\n"
        " def pause(self, entry, prior): self.file.write_text(json.dumps({'paused': True}))\n"
        " def paused(self, entry, prior): return json.loads(self.file.read_text())['paused']\n"
        " def restore(self, entry, prior): self.file.write_text(json.dumps(prior))\n"
        " def inflight(self, entries): return []\n"
        "mw.LinuxBackend = Backend\n"
        "mw.__file__ = __file__\n"
        "raise SystemExit(mw.main())\n"
    )
    root, hermes = tmp_path / "root", tmp_path / "hermes"
    root.mkdir()
    hermes.mkdir()
    state = root / "scheduler.json"
    state.write_text('{"paused": false}')
    journal = tmp_path / "window.json"
    argv = [sys.executable, str(wrapper)]
    options = ["--root", str(root), "--hermes", str(hermes), "--journal", str(journal),
               "--drain-seconds", "0", "--window-seconds", "1.5" if scenario == "expiry" else "20"]

    def command(action, expected_code=0):
        result = subprocess.run([*argv, action, *options], capture_output=True, text=True, timeout=8)
        assert result.returncode == expected_code, result.stdout + result.stderr
        return json.loads(result.stdout)

    keeper = None
    try:
        assert command("enter")["verified"]
        assert command("status")["verified"]
        durable = json.loads(journal.read_text())
        keeper = durable["keeper"]["pid"]
        assert keeper != os.getpid()
        assert json.loads(state.read_text())["paused"]
        if scenario.startswith("crash-"):
            os.kill(keeper, signal.SIGKILL)  # Only this test's temporary, fake keeper.
            # Wait for kernel flock release, including when /proc hides an exited PID.
            import time
            deadline = time.monotonic() + 5
            while True:
                held = False
                for row in durable["locks"]:
                    with Path(row["path"]).open("a+") as contender:
                        try:
                            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            held = True
                if not held:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.01)
            result = command("status", expected_code=2)
            assert not result["verified"]
            assert result["recovery_required"]
            if scenario == "crash-resume":
                assert command("enter")["verified"]
                assert json.loads(journal.read_text())["keeper"]["pid"] != keeper
                assert json.loads(state.read_text())["paused"]
        elif scenario == "expiry":
            import time
            deadline = time.monotonic() + 6
            while json.loads(journal.read_text())["state"] != "closed":
                assert time.monotonic() < deadline, "keeper must restore without an exit command"
                time.sleep(0.025)
            assert json.loads(state.read_text()) == {"paused": False}
        elif scenario == "concurrent-enter":
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(command, ["enter", "enter"]))
            assert all(result["verified"] for result in results)
            assert json.loads(journal.read_text())["keeper"]["pid"] == keeper
        assert command("exit")["state"] == "closed"
        assert json.loads(state.read_text()) == {"paused": False}
    finally:
        if journal.exists():
            subprocess.run([*argv, "exit", *options], capture_output=True, timeout=8)


def test_cli_exit_serializes_stale_keeper_routing_during_crash_resume(tmp_path):
    """Freeze real journal read/admission against a resumed keeper's operation.

    Only transport/backend and scheduling barriers are replaced. Kernel lock
    proof, CLI routing, journal transitions and restoration remain production.
    """
    import os
    import signal
    import subprocess
    import time

    wrapper = tmp_path / "sandbox_keeper.py"
    wrapper.write_text(
        "import sys, json, time\n"
        "from pathlib import Path\n"
        "from contextlib import contextmanager\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "import maintenance_window as mw\n"
        "base = Path(__file__).parent\n"
        "def mark(name): (base / name).touch()\n"
        "def wait(name):\n"
        " deadline = time.monotonic() + 6\n"
        " while not (base / name).exists():\n"
        "  assert time.monotonic() < deadline, name\n"
        "  time.sleep(0.01)\n"
        "class Backend:\n"
        " def __init__(self, root, hermes, journal):\n"
        "  self.root, self.hermes = Path(root).resolve(), Path(hermes).resolve()\n"
        "  self.file = self.root / 'scheduler.json'\n"
        " def inventory(self):\n"
        "  return {'complete': True, 'errors': [], 'entries': [{'kind': 'hermes', 'id': 'fixture', 'command': 'fixture', 'affected': True, 'trace': []}]}\n"
        " def snapshot(self, entry): return json.loads(self.file.read_text())\n"
        " def pause(self, entry, prior):\n"
        "  with (base / 'effects').open('a') as out: out.write('pause\\n')\n"
        "  self.file.write_text(json.dumps({'paused': True}))\n"
        " def paused(self, entry, prior): return json.loads(self.file.read_text())['paused']\n"
        " def restore(self, entry, prior):\n"
        "  data = mw.read_journal(base / 'window.json')\n"
        "  assert mw.locks_verified(data), 'restore must retain source fences'\n"
        "  with (base / 'effects').open('a') as out: out.write('restore\\n')\n"
        "  self.file.write_text(json.dumps(prior))\n"
        " def inflight(self, entries): return []\n"
        "mw.LinuxBackend = Backend\n"
        "mw.__file__ = __file__\n"
        "original_read, original_operation = mw.read_journal, mw.operation_lock\n"
        "is_exit = sys.argv[1] == 'exit' and not (base / 'cleanup').exists()\n"
        "first_read = first_operation = True\n"
        "admitted = False\n"
        "def read(path):\n"
        " global first_read\n"
        " data = original_read(path)\n"
        " if is_exit and first_read:\n"
        "  first_read = False\n"
        "  if not admitted:\n"
        "   assert not mw.locks_verified(data), 'snapshot must name dead keeper'\n"
        "   mark('exit-ready')\n"
        "   wait('exit-go')\n"
        " return data\n"
        "@contextmanager\n"
        "def operation(path, **kwargs):\n"
        " global first_operation, admitted\n"
        " if is_exit and first_operation:\n"
        "  first_operation = False\n"
        "  mark('exit-ready')\n"
        "  wait('exit-go')\n"
        "  mark('exit-attempting')\n"
        " with original_operation(path, **kwargs):\n"
        "  admitted = True\n"
        "  try: yield\n"
        "  finally: admitted = False\n"
        "mw.read_journal, mw.operation_lock = read, operation\n"
        "original_verify = mw.MaintenanceWindow._verify\n"
        "def verify(self, data):\n"
        " result = original_verify(self, data)\n"
        " if result and '--keeper' in sys.argv and (base / 'resume-arm').exists() and not (base / 'resume-held').exists():\n"
        "  mark('resume-held')\n"
        "  wait('resume-go')\n"
        " return result\n"
        "mw.MaintenanceWindow._verify = verify\n"
        "raise SystemExit(mw.main())\n"
    )
    root, hermes = tmp_path / "root", tmp_path / "hermes"
    root.mkdir()
    hermes.mkdir()
    state = root / "scheduler.json"
    state.write_text('{"paused": false}')
    journal = tmp_path / "window.json"
    options = ["--root", str(root), "--hermes", str(hermes), "--journal", str(journal),
               "--drain-seconds", "0", "--window-seconds", "20"]
    argv = [sys.executable, str(wrapper)]

    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, "fixture barrier timeout"
            time.sleep(0.01)

    def command(action, code=0):
        result = subprocess.run([*argv, action, *options], capture_output=True, text=True, timeout=8)
        assert result.returncode == code, result.stdout + result.stderr
        return json.loads(result.stdout)

    exit_process = None
    try:
        assert command("enter")["verified"]
        original = json.loads(journal.read_text())
        os.kill(original["keeper"]["pid"], signal.SIGKILL)  # Disposable fake keeper only.

        def fences_free():
            for row in original["locks"]:
                with Path(row["path"]).open("a+") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return False
            return True

        wait_for(fences_free)
        assert command("status", code=2)["recovery_required"]
        exit_process = subprocess.Popen([*argv, "exit", *options], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
        wait_for(lambda: (tmp_path / "exit-ready").exists())
        (tmp_path / "resume-arm").touch()
        assert command("enter")["verified"]
        wait_for(lambda: (tmp_path / "resume-held").exists())
        resumed = json.loads(journal.read_text())
        assert resumed["keeper"]["pid"] != original["keeper"]["pid"]
        assert json.loads(state.read_text()) == {"paused": True}
        assert (tmp_path / "effects").read_text().splitlines() == ["pause"]
        (tmp_path / "exit-go").touch()
        wait_for(lambda: (tmp_path / "exit-attempting").exists())

        def exit_queued_or_refused():
            with Path(str(journal) + ".operation.lock.admission").open("a+") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True  # Fixed exit has queued behind the held keeper.
            return exit_process.poll() is not None  # Original exit fails immediately.

        wait_for(exit_queued_or_refused)
        (tmp_path / "resume-go").touch()
        stdout, stderr = exit_process.communicate(timeout=8)
        assert exit_process.returncode == 0, stdout + stderr
        assert json.loads(stdout)["state"] == "closed"
        assert json.loads(journal.read_text())["state"] == "closed"
        assert json.loads(state.read_text()) == {"paused": False}
        assert (tmp_path / "effects").read_text().splitlines() == ["pause", "restore"]
        wait_for(fences_free)
    finally:
        (tmp_path / "cleanup").touch()
        (tmp_path / "exit-go").touch()
        (tmp_path / "resume-go").touch()
        if exit_process is not None and exit_process.poll() is None:
            exit_process.communicate(timeout=8)
        if journal.exists():
            subprocess.run([*argv, "exit", *options], capture_output=True, timeout=8)


def test_cli_exit_admits_recovery_after_keeper_closes_during_status(tmp_path, monkeypatch):
    """Freeze the post-handoff race without changing the original crash oracle."""
    original_write = Path.write_text
    barriers = '''
import os, time
base = Path(__file__).parent
is_exit = sys.argv[1] == 'exit'
is_keeper = '--keeper' in sys.argv
def mark(name): (base / name).touch()
def wait(name):
 deadline = time.monotonic() + 5
 while not (base / name).exists():
  assert time.monotonic() < deadline, name
  time.sleep(0.001)
original_read = mw.read_journal
def read(path):
 data = original_read(path)
 if is_exit and data.get('exit_requested') and data['state'] != 'closed' and not (base / 'status-snapshot').exists():
  # This is the real first status snapshot AFTER the admitted keeper handoff.
  assert mw.locks_verified(data)
  (base / 'snapshot.json').write_text(json.dumps(data))
  mark('status-snapshot')
  wait('keeper-released')
 return data
mw.read_journal = read
original_exit = mw.MaintenanceWindow._exit
def keeper_exit(self, data):
 if is_keeper and data.get('exit_requested'):
  wait('status-snapshot')
 return original_exit(self, data)
mw.MaintenanceWindow._exit = keeper_exit
original_release = mw.MaintenanceWindow.release_locks
def release(self):
 closing = is_keeper and self.handles and original_read(self.journal)['state'] == 'closed'
 original_release(self)
 if closing:
  # Closed is durable and fences are free, but the keeper still owns operation.
  for fd in Path('/proc/self/fd').iterdir():
   try:
    if os.readlink(fd) != str(base / 'window.json.operation.lock'):
     continue
    info = (Path('/proc/self/fdinfo') / fd.name).read_text()
    assert 'FLOCK' in info and 'WRITE' in info
    proof = {'pid': os.getpid(), 'device': fd.stat().st_dev, 'inode': fd.stat().st_ino, 'fdinfo': info}
    (base / 'operation-owner.json').write_text(json.dumps(proof))
   except FileNotFoundError:
    continue
  mark('keeper-released')
  wait('recovery-contended')
mw.MaintenanceWindow.release_locks = release
original_flock = mw.fcntl.flock
def flock(fd, operation):
 try:
  return original_flock(fd, operation)
 except BlockingIOError:
  path = os.readlink('/proc/self/fd/' + str(fd))
  if is_exit and path == str(base / 'window.json.operation.lock'):
   assert (base / 'keeper-released').exists()
   assert original_read(base / 'window.json')['state'] == 'closed'
   (base / 'contended-lock.json').write_text(json.dumps({
    'path': path, 'device': os.fstat(fd).st_dev, 'inode': os.fstat(fd).st_ino}))
   mark('recovery-contended')
  raise
mw.fcntl.flock = flock
original_pause, original_restore = Backend.pause, Backend.restore
def pause(self, entry, prior):
 with (base / 'effects').open('a') as out: out.write('pause\\n')
 return original_pause(self, entry, prior)
def restore(self, entry, prior):
 assert mw.locks_verified(original_read(base / 'window.json'))
 with (base / 'effects').open('a') as out: out.write('restore\\n')
 return original_restore(self, entry, prior)
Backend.pause, Backend.restore = pause, restore
'''

    def write(path, body, *args, **kwargs):
        if path == tmp_path / 'sandbox_keeper.py':
            marker = 'raise SystemExit(mw.main())\n'
            assert body.count(marker) == 1
            body = body.replace(marker, barriers + marker)
        return original_write(path, body, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', write)
    # Same real crash/resume, keeper identities, locks, commands and assertions.
    test_detached_cli_keeper_owns_locks_and_recovers_after_real_process_crash(tmp_path, 'crash-resume')
    assert (tmp_path / 'effects').read_text().splitlines() == ['pause', 'restore']
    snapshot = json.loads((tmp_path / 'snapshot.json').read_text())
    closed = json.loads((tmp_path / 'window.json').read_text())
    assert snapshot['state'] == 'active' and snapshot['exit_requested']
    assert closed['state'] == 'closed' and closed['keeper'] == snapshot['keeper']
    contention = json.loads((tmp_path / 'contended-lock.json').read_text())
    lock = tmp_path / 'window.json.operation.lock'
    assert contention == {'path': str(lock), 'device': lock.stat().st_dev, 'inode': lock.stat().st_ino}
    owner = json.loads((tmp_path / 'operation-owner.json').read_text())
    assert owner['pid'] == closed['keeper']['pid']
    assert (owner['device'], owner['inode']) == (contention['device'], contention['inode'])
    for row in closed['locks']:
        with Path(row['path']).open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_source_updates_accept_only_reviewed_before_after_and_detect_drift(module, tmp_path, monkeypatch):
    import hashlib
    import maintenance_inventory

    source = tmp_path / "affected.py"
    source.write_bytes(b"reviewed preimage\n")
    digest = lambda body: hashlib.sha256(body).hexdigest()

    class Backend(FakeBackend):
        def inventory(self):
            result = super().inventory()
            result["entries"][0]["source_sha256"] = {str(source): digest(source.read_bytes())}
            return result

    backend = Backend()
    backend.root, backend.hermes = tmp_path, tmp_path / "hermes"
    monkeypatch.setattr(maintenance_inventory, "affected_paths", lambda *_: [source])
    window = new_window(module, tmp_path, backend)
    try:
        window.enter()
        active = module.ActiveWindow(window.journal, backend)
        old = digest(source.read_bytes())
        new = digest(b"reviewed candidate\n")
        active.accept_file_updates({str(source): old}, {str(source): new})
        assert window.status()["verified"]  # Prepared but not yet written is recoverable.
        source.write_bytes(b"reviewed candidate\n")
        assert window.status()["verified"]
        source.write_bytes(b"unreviewed third value\n")
        assert not window.status()["verified"]
        with pytest.raises(module.WindowError, match="preimage"):
            active.accept_file_updates({str(source): old}, {str(source): new})
        outside = tmp_path / "unrelated.py"
        outside.write_bytes(b"unrelated\n")
        with pytest.raises(module.WindowError, match="outside_adoption_scope"):
            active.accept_file_updates({str(outside): digest(outside.read_bytes())}, {str(outside): new})
        with pytest.raises(module.WindowError):
            window.exit()
        assert all(state["paused"] for state in backend.states.values())
        source.write_bytes(b"reviewed candidate\n")
        active.finish(True)
        window.exit()
    finally:
        window.release_locks()


def test_process_census_excludes_authenticated_controller_but_not_stale_pid(module, tmp_path, monkeypatch):
    root, hermes, proc = tmp_path / "root", tmp_path / "hermes", tmp_path / "proc"
    root.mkdir()
    hermes.mkdir()
    boot = proc / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("fixture-boot\n")
    for pid in (501, 502):
        base = proc / str(pid)
        base.mkdir()
        fields = ["S"] + ["0"] * 18 + ["123"]
        (base / "stat").write_text(str(pid) + " (python) " + " ".join(fields))
        (base / "cwd").symlink_to(root, target_is_directory=True)
        (base / "exe").symlink_to(sys.executable)
        (base / "cmdline").write_bytes(b"python\0" + str(root / "fixture.py").encode() + b"\0")
        (base / "fd").mkdir()
        (base / "maps").write_text("")

    def virtual_path(value):
        path = str(value)
        return proc / path.removeprefix("/proc/") if path.startswith("/proc/") else proc if path == "/proc" else Path(value)

    monkeypatch.setattr(module, "Path", virtual_path)
    import maintenance_admission
    import maintenance_inventory
    original_readers = maintenance_admission.process_readers
    monkeypatch.setattr(maintenance_admission, "process_readers",
                        lambda paths, tasks, owners, excluded: original_readers(paths, tasks, owners, excluded, proc=proc))
    monkeypatch.setattr(maintenance_inventory, "affected_paths", lambda *_: [root / "fixture.py"])
    journal = tmp_path / "window.json"
    data = {"schema": module.SCHEMA, "keeper": {"pid": 500}, "entries": [],
            "controller": {"pid": 501, "start_ticks": 123, "boot_id": "fixture-boot"}}
    module.atomic_json(journal, data)
    backend = module.LinuxBackend(root, hermes, journal)
    assert [item["pid"] for item in backend.inflight([])] == [502]
    data["controller"]["start_ticks"] += 1
    module.atomic_json(journal, data)
    assert sorted(item["pid"] for item in backend.inflight([])) == [501, 502]


def test_controller_command_exemption_is_bound_to_tool_bytes_and_target(module, tmp_path):
    import hashlib

    controller = tmp_path / "maintenance_window.py"
    controller.write_bytes(b"# immutable fixture controller\n")
    root, hermes, journal = tmp_path / "root", tmp_path / "hermes", tmp_path / "window.json"
    data = {"root": str(root), "hermes": str(hermes), "journal": str(journal),
            "control_tools": {str(controller): hashlib.sha256(controller.read_bytes()).hexdigest()}}
    argv = [sys.executable, str(controller), "status", "--root", str(root),
            "--hermes", str(hermes), "--journal", str(journal)]
    assert module.control_process(argv, data)
    assert not module.control_process([*argv[:-1], str(tmp_path / "other.json")], data)
    assert not module.control_process([*argv, "--journal", str(journal)], data)
    controller.write_bytes(b"# changed controller\n")
    assert not module.control_process(argv, data)


def transaction_fixture(module, tmp_path, monkeypatch):
    import hashlib
    import maintenance_inventory

    backend = hermes_backend(module, tmp_path)
    backend.root.mkdir()
    sources = [backend.root / name for name in ("first.py", "second.py")]
    before = [b"old first\n", b"old second\n"]
    after = [b"new first\n", b"new second\n"]
    for path, body in zip(sources, before):
        path.write_bytes(body)
    digest = lambda body: hashlib.sha256(body).hexdigest()
    original_inventory = backend.inventory

    def inventory():
        result = original_inventory()
        result["entries"][0]["source_sha256"] = {str(path): digest(path.read_bytes()) for path in sources}
        return result

    backend.inventory = inventory
    monkeypatch.setattr(maintenance_inventory, "affected_paths", lambda *_: sources)
    window = new_window(module, tmp_path, backend)
    window.enter()
    original_job = backend.job("enabled")
    adopted_job = {**original_job, "script_sha256": "c" * 64}

    def prepare(active):
        active.accept_file_updates({str(path): digest(body) for path, body in zip(sources, before)},
                                   {str(path): digest(body) for path, body in zip(sources, after)})
        active.accept_job_update("enabled", original_job, adopted_job)

    return backend, window, sources, before, after, original_job, adopted_job, prepare


@pytest.mark.parametrize("success", [True, False])
def test_complete_transaction_records_compact_outcome_and_restores(module, tmp_path, monkeypatch, success):
    backend, window, sources, before, after, original_job, adopted_job, prepare = transaction_fixture(module, tmp_path, monkeypatch)
    try:
        active = module.ActiveWindow(window.journal, backend)
        prepare(active)
        for path, body in zip(sources, after):
            path.write_bytes(body)
        backend.jobs["enabled"] = copy.deepcopy(adopted_job)
        if not success:
            for path, body in zip(sources, before):
                path.write_bytes(body)
            backend.jobs["enabled"] = copy.deepcopy(original_job)
        active.finish(success)
        data = module.read_journal(window.journal)
        assert data["source_transaction"]["outcome"] == ("applied" if success else "rolled_back")
        receipt = data["job_updates"]["enabled"]
        assert {"before_sha256", "after_sha256", "before_pin", "after_pin"} <= set(receipt)
        assert not {"before", "after"} & set(receipt)  # Never journal full job payloads.
        assert data["source_transaction"]["job_ids"] == ["enabled"]
        window.exit()
        expected = copy.deepcopy(backend.original)
        if success:
            expected["enabled"]["script_sha256"] = adopted_job["script_sha256"]
        assert backend.jobs == expected
    finally:
        window.release_locks()


@pytest.mark.parametrize("uncertain_kind", ["mixed-sources", "pin-save-landed"])
def test_uncertain_transaction_refuses_exit_without_resuming_jobs(module, tmp_path, monkeypatch, uncertain_kind):
    backend, window, sources, before, after, original_job, adopted_job, prepare = transaction_fixture(module, tmp_path, monkeypatch)
    try:
        active = module.ActiveWindow(window.journal, backend)
        prepare(active)
        if uncertain_kind == "mixed-sources":
            sources[0].write_bytes(after[0])
        else:
            # Files rolled back, but a saver with an uncertain outcome landed the new pin.
            backend.jobs["enabled"] = copy.deepcopy(adopted_job)
        with pytest.raises(module.WindowError, match="adoption_outcome_uncertain"):
            active.finish(False)
        assert module.read_journal(window.journal)["source_transaction"]["outcome"] == "uncertain"
        with pytest.raises(module.WindowError, match="restore_refused"):
            window.exit()
        assert all(job["enabled"] is False for job in backend.jobs.values())
        assert ("resume", "enabled") not in backend.tool_calls
    finally:
        window.release_locks()


@pytest.mark.parametrize("failure", ["adopter-exception", "keeper-lost"])
def test_adoption_guard_refuses_unfinished_full_staging_after_failure(module, tmp_path, monkeypatch, failure):
    backend, window, sources, before, after, original_job, adopted_job, prepare = transaction_fixture(module, tmp_path, monkeypatch)
    monkeypatch.setattr(module, "LinuxBackend", lambda *_: backend)
    monkeypatch.setattr(module, "default_locks", lambda *_: window.lock_paths)
    try:
        with pytest.raises((module.WindowError, SimulatedPowerLoss)):
            with module.require_active_window(window.journal, backend.root, backend.hermes) as active:
                prepare(active)
                for path, body in zip(sources, after):
                    path.write_bytes(body)
                backend.jobs["enabled"] = copy.deepcopy(adopted_job)
                if failure == "adopter-exception":
                    raise SimulatedPowerLoss("adopter did not finish verification")
                window.release_locks()
        assert module.read_journal(window.journal)["source_transaction"]["outcome"] == "uncertain"
        with pytest.raises(module.WindowError, match="restore_refused"):
            window.exit()
        assert all(job["enabled"] is False for job in backend.jobs.values())
    finally:
        window.release_locks()


def test_lost_adopter_without_finish_receipt_cannot_resume_full_staging(module, tmp_path, monkeypatch):
    backend, window, sources, before, after, original_job, adopted_job, prepare = transaction_fixture(module, tmp_path, monkeypatch)
    try:
        active = module.ActiveWindow(window.journal, backend)
        prepare(active)
        for path, body in zip(sources, after):
            path.write_bytes(body)
        backend.jobs["enabled"] = copy.deepcopy(adopted_job)
        # SIGKILL cannot run a context manager's finally or record finish(False).
        data = module.read_journal(window.journal)
        data["source_transaction"]["adopter"]["start_ticks"] += 1
        module.atomic_json(window.journal, data)
        with pytest.raises(module.WindowError, match="restore_refused"):
            window.exit()
        assert all(job["enabled"] is False for job in backend.jobs.values())
    finally:
        window.release_locks()


@pytest.mark.parametrize("success", [True, False])
def test_completed_transaction_cannot_restore_opposite_source_cohort(module, tmp_path, monkeypatch, success):
    backend, window, sources, before, after, original_job, adopted_job, prepare = transaction_fixture(module, tmp_path, monkeypatch)
    try:
        active = module.ActiveWindow(window.journal, backend)
        prepare(active)
        if success:
            for path, body in zip(sources, after):
                path.write_bytes(body)
            backend.jobs["enabled"] = copy.deepcopy(adopted_job)
        active.finish(success)
        for path, body in zip(sources, before if success else after):
            path.write_bytes(body)
        with pytest.raises(module.WindowError, match="restore_refused"):
            window.exit()
        assert all(job["enabled"] is False for job in backend.jobs.values())
    finally:
        window.release_locks()


@pytest.mark.parametrize("success", [True, False])
def test_completed_transaction_cannot_restore_opposite_job_pin(module, tmp_path, monkeypatch, success):
    backend, window, sources, before, after, original_job, adopted_job, prepare = transaction_fixture(module, tmp_path, monkeypatch)
    try:
        active = module.ActiveWindow(window.journal, backend)
        prepare(active)
        if success:
            for path, body in zip(sources, after):
                path.write_bytes(body)
            backend.jobs["enabled"] = copy.deepcopy(adopted_job)
        active.finish(success)
        backend.jobs["enabled"] = copy.deepcopy(original_job if success else adopted_job)
        with pytest.raises(module.WindowError, match="restore_refused"):
            window.exit()
        assert all(job["enabled"] is False for job in backend.jobs.values())
    finally:
        window.release_locks()


def outcome_review(window):
    return {"schema": "glp-maintenance-outcome-review-v1", "decision": "ACCEPT_EXACT_OUTCOME",
            "prepared_by": "fixture-preparer", "reviewer": "independent-fixture-reviewer",
            "proposal": window.status()["outcome_proposal"]}


def test_reviewed_complete_outcome_reacquires_locks_and_restores_adopted_schedule(module, tmp_path, monkeypatch):
    backend, first, sources, before, after, original_job, adopted_job, prepare = transaction_fixture(module, tmp_path, monkeypatch)
    try:
        active = module.ActiveWindow(first.journal, backend)
        prepare(active)
        for path, body in zip(sources, after):
            path.write_bytes(body)
        backend.jobs["enabled"] = copy.deepcopy(adopted_job)
        with pytest.raises(module.WindowError, match="adoption_outcome_uncertain"):
            active.finish(False)
    finally:
        first.release_locks()  # Model a lost keeper before independently reviewed recovery.
    recovered = new_window(module, tmp_path, backend)
    review = outcome_review(recovered)
    assert review["proposal"]["side"] == "after"
    original_restore = backend.restore

    def guarded_restore(entry, prior):
        assert_locks_held(tmp_path)
        return original_restore(entry, prior)

    backend.restore = guarded_restore
    try:
        assert recovered.exit(outcome_review=review)["state"] == "closed"
        expected = copy.deepcopy(backend.original)
        expected["enabled"]["script_sha256"] = adopted_job["script_sha256"]
        assert backend.jobs == expected
        receipt = module.read_journal(recovered.journal)
        assert receipt["source_transaction"]["outcome"] == "applied"
        assert receipt["outcome_review_sha256"] == module.json_sha(review)
        assert_locks_released(tmp_path)
    finally:
        recovered.release_locks()


def test_unreviewed_and_changed_outcome_proposals_never_resume(module, tmp_path, monkeypatch):
    backend, window, sources, before, after, original_job, adopted_job, prepare = transaction_fixture(module, tmp_path, monkeypatch)
    try:
        prepare(module.ActiveWindow(window.journal, backend))
        for path, body in zip(sources, after):
            path.write_bytes(body)
        backend.jobs["enabled"] = copy.deepcopy(adopted_job)
        with pytest.raises(module.WindowError, match="restore_refused"):
            window.exit()
        review = outcome_review(window)
        self_review = {**review, "reviewer": review["prepared_by"]}
        with pytest.raises(module.WindowError, match="independent_exact_outcome_review_required"):
            window.exit(outcome_review=self_review)
        data = module.read_journal(window.journal)
        data["operator_note"] = "journal changed after review"
        module.atomic_json(window.journal, data)
        with pytest.raises(module.WindowError, match="independent_exact_outcome_review_required"):
            window.exit(outcome_review=review)
        assert all(job["enabled"] is False for job in backend.jobs.values())
        assert ("resume", "enabled") not in backend.tool_calls
    finally:
        window.release_locks()


@pytest.mark.parametrize("mixed", ["sources", "pin"])
def test_review_cannot_accept_mixed_source_or_pin_outcome(module, tmp_path, monkeypatch, mixed):
    backend, window, sources, before, after, original_job, adopted_job, prepare = transaction_fixture(module, tmp_path, monkeypatch)
    try:
        prepare(module.ActiveWindow(window.journal, backend))
        sources[0].write_bytes(after[0])
        if mixed == "pin":
            sources[1].write_bytes(after[1])
            # Entire source cohort landed, but the scheduler pin remains before.
        else:
            backend.jobs["enabled"] = copy.deepcopy(adopted_job)
        review = outcome_review(window)
        assert review["proposal"]["side"] is None
        with pytest.raises(module.WindowError, match="independent_exact_outcome_review_required"):
            window.exit(outcome_review=review)
        assert all(job["enabled"] is False for job in backend.jobs.values())
        assert ("resume", "enabled") not in backend.tool_calls
    finally:
        window.release_locks()


def test_linux_control_io_preserves_exact_crontab_line_endings(module, tmp_path, monkeypatch):
    from types import SimpleNamespace
    root = tmp_path / 'root'
    root.mkdir()
    backend = module.LinuxBackend(root, tmp_path / 'hermes', tmp_path / 'window.json')
    body = b'7 * * * * python reader.py\r\n# retained\r\n'
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=body)
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert backend.command(['/usr/bin/crontab', '-l']) == body.decode()
    backend.command(['/usr/bin/crontab', '-'], body.decode())
    assert calls[1][1]['input'] == body
    assert 'text' not in calls[0][1]
