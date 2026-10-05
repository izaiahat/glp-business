#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import re
import socket
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path("/home/ubuntu/business/scripts/hermes_active_work_gate.py")


def load_module():
    spec = importlib.util.spec_from_file_location("hermes_active_work_gate_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def write_proc_stat(proc_root: Path, pid: int, ticks: int, comm: str = "worker (safe)") -> None:
    proc = proc_root / str(pid)
    proc.mkdir(parents=True, exist_ok=True)
    # pid + comm + state, followed by fields 4..22. The 19th token after
    # state is starttime (field 22).
    before_start = ["0"] * 18
    (proc / "stat").write_text(f"{pid} ({comm}) S " + " ".join(before_start + [str(ticks), "0", "0"]) + "\n")


def create_state_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE session_turn_leases (conversation_id TEXT, holder TEXT, acquired_at REAL, expires_at REAL)")
        conn.execute(
            "CREATE TABLE async_delegations (delegation_id TEXT, state TEXT, owner_pid INTEGER, owner_started_at INTEGER)"
        )


def create_execution_db(path: Path, *, with_handoff: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handoff = ", handoff_pending INTEGER" if with_handoff else ""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE executions (id TEXT, job_id TEXT, pid INTEGER, process_started_at INTEGER, status TEXT"
            + handoff
            + ")"
        )


class ActiveWorkGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / ".hermes"
        self.proc = self.root / "proc"
        self.home.mkdir()
        self.proc.mkdir()
        create_state_db(self.home / "state.db")
        create_execution_db(self.home / "cron" / "executions.db")
        (self.home / "cron" / "jobs.json").write_text('{"jobs": []}\n')
        (self.home / "processes.json").write_text("[]\n")
        self.mod = load_module()

    def tearDown(self):
        self.tmp.cleanup()

    def test_proc_stat_parser_handles_spaces_and_parentheses_in_comm(self):
        write_proc_stat(self.proc, 42, 987654, "worker (name) with spaces")
        self.assertEqual(self.mod.process_start_ticks(42, proc_root=self.proc), 987654)

    def test_exact_live_async_owner_blocks_and_pid_reuse_is_stale(self):
        write_proc_stat(self.proc, 101, 500)
        with sqlite3.connect(self.home / "state.db") as conn:
            conn.executemany(
                "INSERT INTO async_delegations VALUES (?, 'running', ?, ?)",
                [("live", 101, 500), ("reused", 101, 499), ("gone", 202, 700)],
            )
        result = self.mod.collect_active_work(home=self.home, proc_root=self.proc, now_epoch=100)
        self.assertFalse(result["ok"])
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["unknown_count"], 0)
        self.assertEqual({row["id"] for row in result["stale_proven"] if row["kind"] == "async_delegation"}, {"reused", "gone"})

    def test_missing_owner_start_ticks_resolves_to_plain_pid_liveness(self):
        """A record without start ticks is still classifiable, never UNKNOWN."""
        write_proc_stat(self.proc, 101, 500)
        with sqlite3.connect(self.home / "state.db") as conn:
            conn.execute("INSERT INTO async_delegations VALUES ('no-ticks-live', 'finalizing', 101, NULL)")
            conn.execute("INSERT INTO async_delegations VALUES ('no-ticks-dead', 'running', 909, NULL)")
        result = self.mod.collect_active_work(
            home=self.home, proc_root=self.proc, now_epoch=100, include_tui_census=False
        )
        self.assertEqual(result["unknown_count"], 0)
        self.assertEqual(result["status"], "SKIPPED_ACTIVE_HERMES_WORK")
        self.assertEqual([row["id"] for row in result["blockers"]], ["no-ticks-live"])
        self.assertEqual(
            result["blockers"][0]["reason"], "pid_present_start_ticks_unverified"
        )
        self.assertEqual([row["id"] for row in result["stale_proven"]], ["no-ticks-dead"])

    def test_unexpired_turn_lease_blocks_without_reading_prompt_data(self):
        with sqlite3.connect(self.home / "state.db") as conn:
            conn.execute("INSERT INTO session_turn_leases VALUES ('session-1', 'holder-secret-not-emitted', 50, 200)")
        result = self.mod.collect_active_work(home=self.home, proc_root=self.proc, now_epoch=100)
        encoded = json.dumps(result)
        self.assertFalse(result["ok"])
        self.assertIn("session-1", encoded)
        self.assertNotIn("holder-secret", encoded)

    def test_stale_cron_rows_do_not_block(self):
        with sqlite3.connect(self.home / "cron" / "executions.db") as conn:
            conn.execute("INSERT INTO executions VALUES ('old', 'job-1', 333, 444, 'running')")
        result = self.mod.collect_active_work(home=self.home, proc_root=self.proc, now_epoch=100)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any(row["kind"] == "cron_execution" for row in result["stale_proven"]))

    def test_live_cron_owner_blocks(self):
        write_proc_stat(self.proc, 333, 444)
        with sqlite3.connect(self.home / "cron" / "executions.db") as conn:
            conn.execute("INSERT INTO executions VALUES ('run', 'job-1', 333, 444, 'claimed')")
        result = self.mod.collect_active_work(home=self.home, proc_root=self.proc, now_epoch=100)
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["blockers"][0]["kind"], "cron_execution")

    def test_handoff_pending_with_a_dead_owner_is_proven_stale(self):
        db = self.home / "cron" / "executions.db"
        db.unlink()
        create_execution_db(db, with_handoff=True)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO executions VALUES ('handoff', 'job-1', 333, 444, 'claimed', 1)")
        result = self.mod.collect_active_work(
            home=self.home, proc_root=self.proc, now_epoch=100, include_tui_census=False
        )
        self.assertEqual(result["status"], "IDLE_SAFE")
        self.assertEqual(result["unknown_count"], 0)
        self.assertEqual([row["id"] for row in result["stale_proven"]], ["handoff"])

    def test_handoff_pending_with_a_live_owner_blocks_as_finite_work(self):
        db = self.home / "cron" / "executions.db"
        db.unlink()
        create_execution_db(db, with_handoff=True)
        write_proc_stat(self.proc, 333, 444)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO executions VALUES ('handoff', 'job-1', 333, 444, 'claimed', 1)")
        result = self.mod.collect_active_work(
            home=self.home, proc_root=self.proc, now_epoch=100, include_tui_census=False
        )
        self.assertEqual(result["status"], "SKIPPED_ACTIVE_HERMES_WORK")
        self.assertTrue(result["blockers"][0]["handoff_pending"])

    def test_cron_claim_owned_by_a_dead_pid_is_proven_stale(self):
        """The exact defect that blocked every night 09-10..09-16."""
        stamp = datetime(2026, 9, 9, 15, 15, 52, tzinfo=timezone.utc).isoformat()
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "job-1", "run_claim": {"at": stamp, "by": f"{socket.gethostname()}:362164"}},
        ]}))
        result = self.mod.collect_active_work(
            home=self.home,
            proc_root=self.proc,
            now_epoch=datetime(2026, 9, 17, tzinfo=timezone.utc).timestamp(),
            include_tui_census=False,
        )
        self.assertEqual(result["status"], "IDLE_SAFE")
        self.assertEqual(result["unknown_count"], 0)
        claim = result["stale_proven"][0]
        self.assertEqual(claim["kind"], "cron_claim")
        self.assertEqual(claim["reason"], "claim_older_than_scheduler_ttl")

    def test_cron_claim_with_a_live_heartbeating_owner_blocks_as_finite_work(self):
        write_proc_stat(self.proc, 4242, 99)
        now = datetime(2026, 9, 17, tzinfo=timezone.utc)
        fresh = (now - timedelta(seconds=14)).isoformat()
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "job-1", "fire_claim": {"at": fresh, "by": f"{socket.gethostname()}:4242:token"}},
        ]}))
        result = self.mod.collect_active_work(
            home=self.home, proc_root=self.proc, now_epoch=now.timestamp(), include_tui_census=False
        )
        self.assertEqual(result["status"], "SKIPPED_ACTIVE_HERMES_WORK")
        self.assertEqual(result["blockers"][0]["reason"], "live_owner_within_ttl")

    def test_cron_claim_past_ttl_is_stale_even_with_a_live_owner_pid(self):
        write_proc_stat(self.proc, 4242, 99)
        now = datetime(2026, 9, 17, tzinfo=timezone.utc)
        old = (now - timedelta(seconds=self.mod.CRON_CLAIM_TTL_FALLBACK_SECONDS + 1)).isoformat()
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "job-1", "run_claim": {"at": old, "by": f"{socket.gethostname()}:4242"}},
        ]}))
        result = self.mod.collect_active_work(
            home=self.home, proc_root=self.proc, now_epoch=now.timestamp(), include_tui_census=False
        )
        self.assertEqual(result["status"], "IDLE_SAFE")
        self.assertEqual(result["stale_proven"][0]["reason"], "claim_older_than_scheduler_ttl")

    def test_claim_ttl_mirror_matches_the_fork_scheduler_literals(self):
        source = self.mod.FORK_CRON_JOBS_SOURCE
        if not source.is_file():
            self.skipTest("fork cron/jobs.py is not present on this host")
        text = source.read_text(encoding="utf-8")
        for name, value in (
            ("ONESHOT_RUN_CLAIM_TTL_SECONDS", int(self.mod.CRON_CLAIM_TTL_FALLBACK_SECONDS)),
            ("_ONESHOT_RUN_CLAIM_TTL_HEADROOM", self.mod.CRON_CLAIM_TTL_HEADROOM),
            ("_DEFAULT_CRON_INACTIVITY_TIMEOUT", self.mod.DEFAULT_CRON_INACTIVITY_TIMEOUT),
        ):
            match = re.search(rf"^{re.escape(name)} = ([0-9.]+)$", text, re.MULTILINE)
            self.assertIsNotNone(match, f"{name} not found in {source}")
            self.assertEqual(float(match.group(1)), float(value), name)

    def test_process_registry_blocks_without_emitting_command(self):
        write_proc_stat(self.proc, 777, 888)
        (self.home / "processes.json").write_text(json.dumps([{
            "session_id": "proc-safe", "command": "SECRET_COMMAND_SHOULD_NOT_APPEAR",
            "pid": 777, "pid_scope": "host", "host_start_time": 888,
            "notify_on_complete": True,
        }]))
        result = self.mod.collect_active_work(home=self.home, proc_root=self.proc, now_epoch=100)
        encoded = json.dumps(result)
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["status"], "SKIPPED_ACTIVE_HERMES_WORK")
        self.assertNotIn("SECRET_COMMAND", encoded)

    def test_profile_state_db_is_included(self):
        profile = self.home / "profiles" / "worker"
        profile.mkdir(parents=True)
        create_state_db(profile / "state.db")
        write_proc_stat(self.proc, 303, 404)
        with sqlite3.connect(profile / "state.db") as conn:
            conn.execute("INSERT INTO async_delegations VALUES ('profile-live', 'running', 303, 404)")
        result = self.mod.collect_active_work(home=self.home, proc_root=self.proc, now_epoch=100)
        self.assertTrue(any(row.get("id") == "profile-live" for row in result["blockers"]))

    def test_corrupt_authority_blocks_as_an_unreadable_source(self):
        (self.home / "processes.json").write_text("{broken")
        result = self.mod.collect_active_work(
            home=self.home, proc_root=self.proc, now_epoch=100, include_tui_census=False
        )
        self.assertEqual(result["status"], "BLOCKED_GATE_SOURCE_UNREADABLE")
        self.assertEqual(result["unreadable_source_count"], 1)
        self.assertEqual(result["unknown_count"], 0)
        self.assertTrue(result["secret_safe"])
        self.assertEqual(result["forbidden_sources_read"], [])

    def test_nightly_finite_active_work_is_clean_skip_exit_zero(self):
        finite = {
            "ok": False,
            "status": "SKIPPED_ACTIVE_HERMES_WORK",
            "active_count": 1,
            "active_finite_count": 1,
            "active_indefinite_count": 0,
            "unknown_count": 0,
            "unreadable_source_count": 0,
            "stale_count": 0,
            "blockers": [{"kind": "managed_process", "classification": "ACTIVE_FINITE"}],
            "stale_proven": [],
            "observations": [],
            "secret_safe": True,
            "forbidden_sources_read": [],
        }
        with mock.patch.object(self.mod, "wait_for_idle", return_value=finite):
            self.assertEqual(self.mod.main(["--home", str(self.home)]), 0)

    def test_unreadable_source_is_nonzero_block(self):
        unreadable = {
            "ok": False,
            "status": "BLOCKED_GATE_SOURCE_UNREADABLE",
            "active_count": 0,
            "active_finite_count": 0,
            "active_indefinite_count": 0,
            "unknown_count": 0,
            "unreadable_source_count": 1,
            "stale_count": 0,
            "blockers": [{"kind": "state_db", "classification": "SOURCE_UNREADABLE"}],
            "stale_proven": [],
            "observations": [],
            "secret_safe": True,
            "forbidden_sources_read": [],
        }
        with mock.patch.object(self.mod, "wait_for_idle", return_value=unreadable):
            self.assertEqual(self.mod.main(["--home", str(self.home)]), 2)


class DetachedAndSupersededTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / ".hermes"
        self.proc = self.root / "proc"
        self.home.mkdir()
        self.proc.mkdir()
        create_state_db(self.home / "state.db")
        (self.home / "cron").mkdir()
        (self.home / "cron" / "jobs.json").write_text('{"jobs": []}\n')
        (self.home / "processes.json").write_text("[]\n")
        self.mod = load_module()

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, **kwargs):
        return self.mod.collect_active_work(
            home=self.home, proc_root=self.proc, include_tui_census=False, **kwargs
        )

    def test_tui_under_a_bash_wrapper_still_resolves_its_tmux_session_name(self):
        """The updater relaunches each seat as `bash -lc '... hermes --tui ...'`,
        so the pane pid is the WRAPPER, not the TUI. A direct pid->pane lookup
        then yields no session_name and the census reports nameless TUIs - which
        is exactly what happened after the 2026-09-17 relaunch. Resolve the pane
        by walking up to the first ancestor that owns one."""
        wrapper_pid, tui_pid = 4001, 4002
        argv = ("/home/ubuntu/.hermes/hermes-agent/venv/bin/python\x00"
                "/home/ubuntu/.hermes/hermes-agent/venv/bin/hermes\x00--tui\x00"
                "--in\x00/home/ubuntu/business\x00--resume\x0020260915_000822_653900\x00")
        for pid, ppid in ((wrapper_pid, 1), (tui_pid, wrapper_pid)):
            d = self.proc / str(pid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "stat").write_text(
                f"{pid} (python) S {ppid} " + " ".join(["0"] * 18) + " 123456 " + " ".join(["0"] * 30))
            (d / "cmdline").write_text(argv if pid == tui_pid else "bash\x00-lc\x00hermes --tui\x00")
        (d / "cmdline").write_text(argv)

        def tmux_runner(command, **kwargs):
            import subprocess as sp
            out = f"glp-ads\t%71\t{wrapper_pid}\n"
            return sp.CompletedProcess(args=command, returncode=0, stdout=out, stderr="")

        def capture_runner(command, **kwargs):
            import subprocess as sp
            return sp.CompletedProcess(args=command, returncode=0,
                                       stdout=" - ready | gpt 6 astra | voice off\n", stderr="")

        census = self.mod.tui_census(proc_root=self.proc, tmux_runner=tmux_runner,
                                     capture_runner=capture_runner)
        named = {t.get("session_name") for t in census["tuis"]}
        self.assertIn("glp-ads", named, census)
        self.assertEqual(census["unclassified_count"], 0, census)

    def test_detached_background_service_is_recorded_but_never_blocks(self):
        """notify_on_complete false means no turn is waiting on this process."""
        write_proc_stat(self.proc, 603684, 409171402)
        (self.home / "processes.json").write_text(json.dumps([{
            "session_id": "proc_detached",
            "command": "SECRET_COMMAND_SHOULD_NOT_APPEAR",
            "pid": 603684, "pid_scope": "host", "host_start_time": 409171402,
            "notify_on_complete": False, "session_key": "20260915_000822_653900",
        }]))
        result = self.collect(now_epoch=100)
        encoded = json.dumps(result)
        self.assertEqual(result["status"], "IDLE_SAFE")
        self.assertTrue(result["ok"])
        self.assertEqual(result["detached_count"], 1)
        self.assertEqual(result["detached_services"][0]["classification"], "DETACHED_SERVICE")
        self.assertNotIn("SECRET_COMMAND", encoded)

    def test_awaited_background_process_still_blocks(self):
        write_proc_stat(self.proc, 555, 666)
        (self.home / "processes.json").write_text(json.dumps([{
            "session_id": "proc_awaited", "pid": 555, "pid_scope": "host",
            "host_start_time": 666, "notify_on_complete": True,
        }]))
        result = self.collect(now_epoch=100)
        self.assertEqual(result["status"], "SKIPPED_ACTIVE_HERMES_WORK")
        self.assertEqual(result["detached_count"], 0)

    def test_non_host_pid_scope_is_classified_by_its_owning_session(self):
        with sqlite3.connect(self.home / "state.db") as conn:
            conn.execute("INSERT INTO session_turn_leases VALUES ('live-session', 'h', 1, 500)")
        (self.home / "processes.json").write_text(json.dumps([
            {"session_id": "a", "pid_scope": "container", "session_key": "live-session"},
            {"session_id": "b", "pid_scope": "container", "session_key": "dead-session"},
        ]))
        result = self.collect(now_epoch=100)
        self.assertEqual(result["unknown_count"], 0)
        blocked = {row["id"] for row in result["blockers"] if row["kind"] == "managed_process"}
        stale = {row["id"] for row in result["stale_proven"] if row["kind"] == "managed_process"}
        self.assertEqual(blocked, {"a"})
        self.assertEqual(stale, {"b"})

    def test_orphaned_in_gateway_execution_is_stale_without_a_live_claim(self):
        """The owner pid is the long-lived gateway, so PID identity alone can
        never expire. A newer terminal row for the same job proves it orphaned."""
        db = self.home / "cron" / "executions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE executions (id TEXT, job_id TEXT, pid INTEGER, "
                "process_started_at INTEGER, status TEXT, claimed_at TEXT)"
            )
            conn.execute(
                "INSERT INTO executions VALUES ('orphan','job-1',3101750,359516535,'running','2026-09-16T20:56:14-07:00')"
            )
            conn.execute(
                "INSERT INTO executions VALUES ('newer','job-1',3101750,359516535,'completed','2026-09-16T21:06:14-07:00')"
            )
        write_proc_stat(self.proc, 3101750, 359516535)
        now = datetime(2026, 9, 16, 21, 10, tzinfo=timezone.utc).timestamp()
        result = self.collect(now_epoch=now)
        self.assertEqual(result["status"], "IDLE_SAFE")
        self.assertEqual(result["stale_proven"][0]["reason"], "superseded_by_newer_terminal_execution")

    def test_execution_with_a_live_job_claim_still_blocks(self):
        db = self.home / "cron" / "executions.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE executions (id TEXT, job_id TEXT, pid INTEGER, "
                "process_started_at INTEGER, status TEXT, claimed_at TEXT)"
            )
            conn.execute(
                "INSERT INTO executions VALUES ('live','job-1',3101750,359516535,'running','2026-09-16T20:56:14-07:00')"
            )
        write_proc_stat(self.proc, 3101750, 359516535)
        now = datetime(2026, 9, 17, 3, 57, tzinfo=timezone.utc)
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "job-1", "fire_claim": {
                "at": (now - timedelta(seconds=14)).isoformat(),
                "by": f"{socket.gethostname()}:3101750:token",
            }},
        ]}))
        result = self.collect(now_epoch=now.timestamp())
        self.assertEqual(result["status"], "SKIPPED_ACTIVE_HERMES_WORK")
        reasons = {row.get("reason") for row in result["blockers"]}
        self.assertIn("job_claim_heartbeat_is_live", reasons)


class TuiCensusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.mod = load_module()

    def tearDown(self):
        self.tmp.cleanup()

    def write_proc(self, pid, argv, ppid=1, ticks=100):
        write_proc_stat(self.proc, pid, ticks)
        stat = (self.proc / str(pid) / "stat").read_text().split(") ", 1)[1].split()
        stat[1] = str(ppid)
        (self.proc / str(pid) / "stat").write_text(f"{pid} (proc) " + " ".join(stat) + "\n")
        (self.proc / str(pid) / "cmdline").write_bytes(b"\0".join(x.encode() for x in argv) + b"\0")

    def fake_tmux(self, pane_rows, capture_text):
        class Proc:
            def __init__(self, stdout):
                self.stdout = stdout
                self.returncode = 0

        def runner(cmd, timeout=None):
            if cmd[1] == "list-panes":
                return Proc("\n".join(pane_rows) + "\n")
            return Proc(capture_text)

        return runner

    def test_both_cli_shapes_are_recognised_as_live_tuis(self):
        self.write_proc(100, ["/venv/bin/python", "/venv/bin/hermes", "--tui", "--in", "/w", "--resume", "sid-a"])
        self.write_proc(101, ["/venv/bin/python", "/venv/bin/hermes", "chat", "--tui", "--resume", "sid-b"])
        found = {row["pid"]: row for row in self.mod.hermes_tui_processes(proc_root=self.proc)}
        self.assertEqual(set(found), {100, 101})
        self.assertEqual(found[100]["argv_shape"], "flag")
        self.assertEqual(found[100]["resume_id"], "sid-a")
        self.assertEqual(found[101]["argv_shape"], "chat")

    def test_idle_ready_pane_with_no_turn_signal_is_idle(self):
        self.write_proc(100, ["/venv/bin/hermes", "--tui", "--in", "/w", "--resume", "sid-a"])
        census = self.mod.tui_census(
            proc_root=self.proc,
            tmux_runner=self.fake_tmux(["glp-ads\t%71\t100"], "─ ready │\n"),
            capture_runner=self.fake_tmux(["glp-ads\t%71\t100"], "─ ready │\n"),
        )
        self.assertEqual(census["tui_count"], 1)
        self.assertEqual(census["tui_idle_count"], 1)
        self.assertEqual(census["unclassified_count"], 0)
        self.assertEqual(census["tuis"][0]["state"], "IDLE")
        self.assertEqual(census["tuis"][0]["session_name"], "glp-ads")

    def test_seat_turn_descendant_makes_a_tui_active(self):
        self.write_proc(100, ["/venv/bin/hermes", "--tui", "--resume", "sid-a"])
        self.write_proc(140, ["python", "-m", "hermes_seat_turn", "--session", "sid-a"], ppid=100)
        census = self.mod.tui_census(
            proc_root=self.proc,
            tmux_runner=self.fake_tmux(["glp-ads\t%71\t100"], "─ ready │\n"),
            capture_runner=self.fake_tmux(["glp-ads\t%71\t100"], "─ ready │\n"),
        )
        self.assertEqual(census["tuis"][0]["state"], "ACTIVE")
        self.assertEqual(census["tuis"][0]["signals"]["seat_turn_pids"], [140])

    def test_unexpired_turn_lease_makes_a_tui_active(self):
        self.write_proc(100, ["/venv/bin/hermes", "--tui", "--resume", "sid-a"])
        census = self.mod.tui_census(
            proc_root=self.proc,
            live_session_keys={"sid-a"},
            tmux_runner=self.fake_tmux(["glp-ads\t%71\t100"], "─ ready │\n"),
            capture_runner=self.fake_tmux(["glp-ads\t%71\t100"], "─ ready │\n"),
        )
        self.assertEqual(census["tuis"][0]["state"], "ACTIVE")

    def test_working_pane_is_active_and_a_failed_capture_is_never_unknown(self):
        self.write_proc(100, ["/venv/bin/hermes", "--tui", "--resume", "sid-a"])
        working = self.fake_tmux(["glp-ads\t%71\t100"], "Running tool esc to interrupt\n")
        census = self.mod.tui_census(proc_root=self.proc, tmux_runner=working, capture_runner=working)
        self.assertEqual(census["tuis"][0]["state"], "ACTIVE")

        def broken(cmd, timeout=None):
            if cmd[1] == "list-panes":
                class P:
                    stdout = "glp-ads\t%71\t100\n"
                    returncode = 0
                return P()
            raise RuntimeError("capture failed")

        census = self.mod.tui_census(proc_root=self.proc, tmux_runner=broken, capture_runner=broken)
        self.assertEqual(census["tuis"][0]["state"], "IDLE")
        self.assertEqual(census["unclassified_count"], 0)

    def test_claude_code_panes_are_never_part_of_the_census(self):
        self.write_proc(100, ["node", "/usr/bin/claude", "--resume", "e6be5500"])
        census = self.mod.tui_census(
            proc_root=self.proc,
            tmux_runner=self.fake_tmux(["fable-operator\t%0\t100"], "─ ready │\n"),
            capture_runner=self.fake_tmux(["fable-operator\t%0\t100"], "─ ready │\n"),
        )
        self.assertEqual(census["tui_count"], 0)


class WaitForIdleTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_finite_work_is_waited_out_then_admitted(self):
        busy = {"ok": False, "status": "SKIPPED_ACTIVE_HERMES_WORK"}
        idle = {"ok": True, "status": "IDLE_SAFE"}
        calls = []

        def fake_collect(**kwargs):
            calls.append(kwargs)
            return dict(busy) if len(calls) < 3 else dict(idle)

        with mock.patch.object(self.mod, "collect_active_work", side_effect=fake_collect), \
             mock.patch.object(self.mod.time, "sleep"):
            result = self.mod.wait_for_idle(timeout_seconds=600, poll_seconds=0.01)
        self.assertEqual(result["status"], "IDLE_SAFE")
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["waited_polls"], 2)

    def test_a_hard_block_is_never_waited_on(self):
        blocked = {"ok": False, "status": "BLOCKED_ACTIVE_HERMES_WORK"}
        with mock.patch.object(self.mod, "collect_active_work", return_value=dict(blocked)) as collect, \
             mock.patch.object(self.mod.time, "sleep") as slept:
            result = self.mod.wait_for_idle(timeout_seconds=600, poll_seconds=0.01)
        self.assertEqual(result["status"], "BLOCKED_ACTIVE_HERMES_WORK")
        self.assertEqual(collect.call_count, 1)
        slept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
