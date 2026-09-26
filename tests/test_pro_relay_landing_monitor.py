import contextlib
import fcntl
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import importlib.util
import sys
_spec = importlib.util.spec_from_file_location('scripts._pro_relay_monitor_test', Path(__file__).resolve().parents[1] / 'scripts/pro_relay_landing_monitor.py')
monitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitor)
sys.modules['pro_relay_landing_monitor'] = monitor


import pytest


@pytest.fixture(autouse=True)
def deny_network():
    with patch('socket.socket.connect', side_effect=AssertionError('NETWORK FORBIDDEN IN RELAY TESTS')), patch('socket.create_connection', side_effect=AssertionError('NETWORK FORBIDDEN IN RELAY TESTS')):
        yield


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / monitor.JOBS
        self.jobs.mkdir(parents=True)
        self.ledger = self.root / monitor.LEDGER
        self.clock = 2000000000.0
        clock = patch.object(monitor.time, 'time', side_effect=lambda: self.clock)
        clock.start(); self.addCleanup(clock.stop)
        self.sent, self.fetched = [], []
        self.wave = self.root / "wave.json"
        self.wave.write_text(json.dumps({"jobs": [{"job": "alpha"}]}), encoding="utf-8")
        self.ps = f"101 /usr/bin/python3 -u scripts/gpt6pro_wave.py --wave {self.wave} --account a\n"
        guard = patch.object(monitor.subprocess, "run", side_effect=AssertionError("unexpected external call"))
        guard.start()
        self.addCleanup(guard.stop)

    def job(self, name="alpha", status="running"):
        path = self.jobs / name / "JOB.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"status": status, "account": "a"}), encoding="utf-8")
        return path

    def send(self, line):
        self.sent.append(line)
        return {"sent": True, "message_id": "123"}

    def fetch(self, job):
        self.fetched.append(job)
        outbox = self.jobs / job / "outbox"
        outbox.mkdir(exist_ok=True)
        (outbox / "response.md").write_text("fixture", encoding="utf-8")
        return 0

    def tick(self, **kwargs):
        try:
            return monitor.once(self.root, ps_reader=lambda: self.ps,
                                fetcher=kwargs.get("fetcher", self.fetch), sender=kwargs.get("sender", self.send))
        finally:
            self.clock += 121

    def records(self, kind=None):
        rows = [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if kind is None or row["kind"] == kind]

    def test_transition_once_across_restart(self):
        self.job()
        self.tick()
        path = self.job(status="done")
        original = path.read_bytes()
        self.tick()
        self.tick()
        events = [r for r in self.records("event") if r.get("job") == "alpha"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["key"], "alpha:done")
        self.assertEqual(events[0]["previous"], "running")
        self.assertEqual(self.fetched, ["alpha"])
        self.assertEqual(self.sent, [])
        lines = (self.root / 'ops/orchestrator-events.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(set(event), {'ts', 'kind', 'job', 'account', 'outbox_path'})
        self.assertEqual(event['kind'], 'pro_landed')
        self.assertEqual(event['job'], 'alpha')
        self.assertEqual(event['account'], 'a')
        self.assertEqual(event['outbox_path'], str(self.jobs / 'alpha/outbox'))
        self.assertEqual(path.read_bytes(), original)
        written = {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(written, {str(monitor.LEDGER), str(monitor.JOBS / "alpha/JOB.json"),
                                   str(monitor.JOBS / "alpha/outbox/response.md"), str(monitor.EVENTS), "wave.json"})

    def test_failed_and_error_once_without_fetch(self):
        self.job()
        self.tick()
        for state in ("failed", "error"):
            self.job(status=state)
            self.tick()
            self.tick()
        self.assertEqual([r["key"] for r in self.records("event")], ["alpha:failed", "alpha:error"])
        self.assertEqual(self.fetched, [])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(line.startswith("PRO_FAILED ") for line in self.sent))

    def test_bootstrap_done_is_not_silently_seeded(self):
        self.job(status="done")
        self.tick()
        self.assertEqual(self.records("event")[0]["previous"], "UNKNOWN")
        self.assertEqual(self.fetched, ["alpha"])

    def test_dead_runner_recovery_and_recurrence(self):
        self.job()
        live = self.ps
        self.ps = ""
        self.assertEqual(self.tick(), 1)
        self.tick()
        self.assertEqual(sum(line.startswith("PRO_RUNNER_DEAD ") for line in self.sent), 1)
        self.ps = live
        self.assertEqual(self.tick(), 0)
        self.ps = ""
        self.tick()
        self.assertEqual(sum(line.startswith("PRO_RUNNER_DEAD ") for line in self.sent), 1)
        self.tick()
        self.assertEqual(sum(line.startswith("PRO_RUNNER_DEAD ") for line in self.sent), 2)

    def test_other_account_does_not_mask_dead_runner(self):
        self.job("orphan")
        self.tick()
        self.assertEqual(self.sent, [])  # transient dead runner is journaled, not sent
        self.tick()
        self.assertTrue(any("PRO_RUNNER_DEAD job=orphan " in line for line in self.sent))

    def test_collision_once_per_episode(self):
        self.job()
        single = self.ps
        self.ps += f"102 python3 -B -m scripts.gpt6pro_wave --account=a --wave={self.wave}\n"
        self.assertEqual(self.tick(), 1)
        self.tick()
        collisions = [line for line in self.sent if line.startswith("COLLISION ")]
        self.assertEqual(len(collisions), 1)
        self.assertIn("pids=101,102", collisions[0])
        self.ps = single
        self.tick()
        self.ps += f"103 python3 scripts/gpt6pro_wave.py --wave {self.wave} --account a\n"
        self.tick()
        self.assertEqual(sum(line.startswith("COLLISION ") for line in self.sent), 2)

    def test_distinct_accounts_and_non_runners_do_not_collide(self):
        self.job()
        self.ps += f"102 python3 scripts/gpt6pro_wave.py --wave {self.wave} --account b\n"
        self.ps += f"103 /bin/sh -c 'python3 scripts/gpt6pro_wave.py --wave {self.wave} --account a'\n"
        self.ps += "104 python3 -c 'print(\"gpt6pro_wave.py\")'\n"
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(monitor.runners(self.ps)), 2)
        self.assertEqual(self.sent, [])

    def test_disjoint_waves_on_one_account_are_not_collisions(self):
        self.job()
        other = self.root / 'other-wave.json'
        other.write_text(json.dumps({'jobs': [{'job': 'beta'}]}))
        self.ps += f'102 python3 scripts/gpt6pro_wave.py --wave {other} --account a\n'
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.sent, [])

    def test_wrong_account_runner_does_not_cover_job(self):
        self.job(); self.ps = self.ps.replace('--account a', '--account b')
        self.tick(); self.tick()
        self.assertTrue(any(line.startswith('PRO_RUNNER_DEAD ') for line in self.sent))

    def test_duplicate_pid_is_not_collision(self):
        self.job()
        self.ps += self.ps
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(monitor.runners(self.ps)), 1)

    def test_unknown_process_state_is_not_dead_or_healthy(self):
        self.job()
        with patch.object(monitor, "runners", side_effect=ValueError("bad ps")):
            self.assertEqual(self.tick(), 1)
        self.assertTrue(any(line.startswith("PRO_UNVERIFIED PRO_RUNNER_UNKNOWN ") for line in self.sent))
        self.assertFalse(any(line.startswith("PRO_RUNNER_DEAD ") for line in self.sent))

    def test_unreadable_wave_and_implicit_account_are_unknown(self):
        self.job()
        self.ps = f"101 python3 scripts/gpt6pro_wave.py --wave {self.root / 'missing.json'}\n"
        self.assertEqual(self.tick(), 1)
        self.assertTrue(any("implicit_account=UNKNOWN" in line for line in self.sent))
        self.assertFalse(any(line.startswith("PRO_RUNNER_DEAD ") for line in self.sent))

    def test_bad_job_does_not_hide_good_landing(self):
        self.job("broken").write_text("{", encoding="utf-8")
        self.job(status="done")
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.fetched, ["alpha"])
        self.assertTrue(any(line.startswith("PRO_UNVERIFIED PRO_INPUT_UNKNOWN ") for line in self.sent))

    def test_outbox_error_does_not_hide_terminal_state(self):
        self.job(status="done")
        (self.jobs / "alpha/outbox").write_text("not a directory", encoding="utf-8")
        self.assertEqual(self.tick(fetcher=lambda job: 1), 1)
        self.assertTrue(any(r.get("key") == "alpha:done" for r in self.records("event")))
        self.assertTrue(any(line.startswith("PRO_UNVERIFIED PRO_INPUT_UNKNOWN ") for line in self.sent))

    def test_ps_failure_is_unknown_not_no_runners(self):
        self.job()

        def failed_ps():
            raise subprocess.CalledProcessError(1, ["ps"])

        self.assertEqual(monitor.once(self.root, ps_reader=failed_ps, fetcher=self.fetch, sender=self.send), 1)
        self.assertTrue(any(line.startswith("PRO_UNVERIFIED PRO_RUNNER_UNKNOWN ") for line in self.sent))
        self.assertFalse(any(line.startswith("PRO_RUNNER_DEAD ") for line in self.sent))

    def test_missing_outbox_terminal_after_two_verified_absences_no_promotion(self):
        path = self.job(status='done')
        (path.parent / 'RESPONSE.md').write_text('advisory only')
        original = path.read_bytes()
        calls = []
        def absent(job):
            calls.append(job)
            return 10  # relay's exact verified-zero-outbox return code
        self.assertEqual(self.tick(fetcher=absent), 1)
        self.assertEqual(self.tick(fetcher=absent), 0)
        self.assertEqual(self.tick(fetcher=absent), 0)
        self.assertEqual(calls, ['alpha', 'alpha'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((path.parent / 'RESPONSE.md').read_text(), 'advisory only')
        self.assertFalse((path.parent / 'outbox').exists())
        self.assertFalse((self.root / monitor.EVENTS).exists())
        self.assertEqual(self.records('action')[-1]['state'], 'OUTBOX_ABSENT')

    def test_fetch_failure_retries_without_second_alert(self):
        self.job(status="done")
        calls = []

        def fetch(job):
            calls.append(job)
            return 1 if len(calls) == 1 else 0

        self.assertEqual(self.tick(fetcher=fetch), 1)
        self.assertIn("fetch=FAILED", self.sent[0])
        self.assertEqual(self.tick(fetcher=fetch), 0)
        self.tick(fetcher=fetch)
        self.assertEqual(calls, ["alpha", "alpha"])
        self.assertEqual(len(self.sent), 1)

    def test_missing_token_is_retryable(self):
        self.job(status="failed")
        self.assertEqual(self.tick(sender=lambda line: {"sent": False, "reason": "missing_discord_token_env"}), 1)
        self.assertEqual(self.tick(), 0)
        self.tick()
        self.assertEqual(len(self.sent), 1)

    def test_ambiguous_delivery_is_not_retried(self):
        self.job(status="failed")
        attempts = []

        def sender(line):
            attempts.append(line)
            return {"sent": False, "reason": "TimeoutError"}

        self.assertEqual(self.tick(sender=sender), 1)
        self.assertEqual(self.tick(sender=sender), 1)
        self.assertEqual(len(attempts), 1)
        text, rc = monitor.status(self.root, ps_reader=lambda: self.ps)
        self.assertEqual(rc, 1)
        self.assertIn('"UNKNOWN": 1', text)

    def test_claim_survives_interruption_without_replay(self):
        self.job(status="failed")
        attempts = []

        def interrupted(line):
            attempts.append(line)
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.tick(sender=interrupted)
        self.assertEqual(self.tick(), 1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.records("event")), 1)

    def test_fetch_interruption_is_visible_and_not_replayed(self):
        self.job(status="done")

        def interrupted(job):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.tick(fetcher=interrupted)
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.fetched, [])
        self.assertIn("fetch=UNKNOWN", self.sent[0])

    def test_overlapping_invocation_is_locked_before_effects(self):
        self.job(status="done")
        self.ledger.parent.mkdir(parents=True)
        with self.ledger.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.tick()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.ledger.read_bytes(), b"")

    def test_corrupt_ledger_fails_closed(self):
        self.job(status="done")
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_bytes(b'{"kind": "event"')
        original = self.ledger.read_bytes()
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(self.ledger.read_bytes(), original)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fetched, [])

    def test_status_is_read_only_and_one_screen(self):
        self.job(status="done")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        text, rc = monitor.status(self.root, ps_reader=lambda: self.ps)
        self.assertEqual(rc, 1)
        self.assertIn("last_scan=NEVER", text)
        self.assertIn("unseen_terminal=1", text)
        self.assertLessEqual(len(text.splitlines()), 24)
        self.assertTrue(all(len(line) <= 120 for line in text.splitlines()))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertFalse(self.ledger.exists())

    def test_install_cron_only_prints_and_verbs_are_exclusive(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(monitor, "once", side_effect=AssertionError):
            self.assertEqual(monitor.main(["--install-cron"]), 0)
        self.assertEqual(output.getvalue(), monitor.CRON + "\n")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            monitor.main(["--once", "--status"])
        self.assertFalse(self.ledger.exists())

    def test_wake_append_reconciles_crash_without_duplicate(self):
        self.job(status='done')
        original = monitor.wake_orchestrator
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch.object(monitor, 'wake_orchestrator', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): self.tick()
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(len((self.root / monitor.EVENTS).read_text().splitlines()), 1)
        self.assertEqual(self.fetched, ['alpha'])

    def test_wake_claim_without_append_stays_unknown_no_blind_retry(self):
        self.job(status='done')
        with patch.object(monitor, 'wake_orchestrator', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt): self.tick()
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.tick(), 1)
        self.assertEqual((self.root / monitor.EVENTS).read_text(), '')
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith('PRO_WAKE_FAILED '))

    def test_failed_fetch_never_emits_landing_event(self):
        self.job(status='done')
        self.tick(fetcher=lambda job: 1)
        self.tick(fetcher=lambda job: 1)
        self.assertFalse((self.root / monitor.EVENTS).exists())
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith('PRO_UNVERIFIED '))

    def test_recovered_fetch_wakes_after_failure_alert(self):
        self.job(status='done')
        self.tick(fetcher=lambda job: 1)
        self.assertEqual(self.tick(), 0)
        self.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len((self.root / monitor.EVENTS).read_text().splitlines()), 1)

    def test_transient_dead_runner_clears_without_alert(self):
        self.job(); live = self.ps; self.ps = ''
        self.tick(); self.ps = live; self.tick(); self.tick()
        self.assertEqual(self.sent, [])

    def test_production_adapters_use_only_documented_commands(self):
        with patch.object(monitor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="fixture")) as run:
            self.assertEqual(monitor.read_ps(), "fixture")
            self.assertEqual(run.call_args.args[0], ["ps", "-ww", "-eo", "pid=,args="])
            self.assertEqual(monitor.fetch_outbox(self.root, "alpha"), 0)
            self.assertEqual(run.call_args.args[0], [monitor.sys.executable, "-B",
                             str(self.root / "scripts/gpt6pro_relay.py"), "fetch-outbox", "--job", "alpha"])
            self.assertEqual(run.call_args.kwargs["timeout"], 900)
            self.assertEqual(run.call_args.kwargs["cwd"], self.root)
        with patch("scripts.hermes_ops_alert.send_ops_alert", return_value={"sent": True}) as send:
            self.assertEqual(monitor.send_alert("PRO_FAILED fixture"), {"sent": True})
            send.assert_called_once_with("PRO_FAILED fixture")


if __name__ == "__main__":
    unittest.main()


class BaselineTests(unittest.TestCase):
    """--baseline adopts existing terminal jobs: the next --once announces and fetches nothing historical."""

    def test_baseline_then_once_has_no_external_effects(self):
        import json as _json, tempfile as _tf
        from pathlib import Path as _P
        import pro_relay_landing_monitor as m
        with _tf.TemporaryDirectory() as tmp:
            root = _P(tmp); jobs = root / "reports/ops/gpt6pro-relay/jobs"
            for name, st in (("old-a", "done"), ("old-b", "failed"), ("live-c", "running")):
                (jobs / name).mkdir(parents=True); (jobs / name / "JOB.json").write_text(_json.dumps({"job": name, "status": st, "account": "x"}))
            (root / "ops/gpt6pro-relay").mkdir(parents=True, exist_ok=True)
            self.assertGreater(m.adopt(root, jobs, ps_reader=lambda: ""), 0)
            sent, fetched = [], []
            rc = m.once(root, jobs, ps_reader=lambda: "", fetcher=lambda job: (fetched.append(job), 0)[1], sender=lambda line: (sent.append(line), {"sent": True})[1])
            landed = lambda: [l for l in sent if l.startswith(("PRO_LANDED", "PRO_FAILED"))]
            self.assertEqual(landed(), []); self.assertEqual(fetched, [])
            # a NEW landing after the baseline is announced exactly once
            (jobs / "live-c" / "JOB.json").write_text(_json.dumps({"job": "live-c", "status": "done", "account": "x"}))
            m.once(root, jobs, ps_reader=lambda: "", fetcher=lambda job: (fetched.append(job), 0)[1], sender=lambda line: (sent.append(line), {"sent": True})[1])
            self.assertEqual(len(landed()), 0); self.assertEqual(fetched, ["live-c"])
            m.once(root, jobs, ps_reader=lambda: "", fetcher=lambda job: (fetched.append(job), 0)[1], sender=lambda line: (sent.append(line), {"sent": True})[1])
            self.assertEqual(len(landed()), 0)
            events = [json.loads(line) for line in (root / m.EVENTS).read_text().splitlines()]
            self.assertEqual([r['job'] for r in events], ['live-c'])
