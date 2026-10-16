"""Offline owner regression and unchanged bounded cgroup enforcement."""
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "ops/hermes-cron-scripts/cgroup_headroom_guardian.py"
GIB = 1024 ** 3
HOME = "/home/ubuntu/.hermes"
RUNTIME = HOME + "/hermes-agent"
WORKSPACE = "/home/ubuntu/business"
SESSION = "20260922_191215_fffbbb"


def load():
    spec = importlib.util.spec_from_file_location("guardian", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_owner(tmp_path, monkeypatch):
    module = load()
    proc = tmp_path / "proc"
    scope = tmp_path / "tmux-spawn-test.scope"
    scope.mkdir()
    (scope / "cgroup.procs").write_text("100\n101\n102\n")
    commands = {
        100: [RUNTIME + "/venv/bin/python", RUNTIME + "/venv/bin/hermes", "--profile", "default", "chat", "--tui", "--source", "claude-code", "--resume", SESSION, "--model", "gpt-6-astra-900k"],
        101: [HOME + "/node/bin/node", "--expose-gc", RUNTIME + "/ui-tui/dist/entry.js"],
        102: [RUNTIME + "/venv/bin/python", "-m", "tui_gateway.entry"],
    }
    for pid, argv in commands.items():
        p = proc / str(pid)
        p.mkdir(parents=True)
        (p / "cmdline").write_bytes(b"\0".join(s.encode() for s in argv) + b"\0")
        # fields 3..22 include ppid and starttime (fixed stable identities).
        (p / "stat").write_text(f"{pid} (python) S {pid - 1} " + "0 " * 17 + str(pid))
        (p / "cwd").symlink_to(WORKSPACE)
        (p / "environ").write_bytes(b"HERMES_HOME=" + HOME.encode() + b"\0")
    original_path = Path
    monkeypatch.setattr(module, "Path", lambda value: proc / str(value).removeprefix("/proc/") if str(value).startswith("/proc/") else original_path(value))
    monkeypatch.setattr(module, "PROC_ROOT", proc, raising=False)
    return module, proc, scope


def test_current_persistent_coordinator_uses_existing_glp_policy(tmp_path, monkeypatch):
    module, _, scope = fixture_owner(tmp_path, monkeypatch)
    assert module.scope_owner(scope) == ("--source glp-scout", "glp-scout")
    assert module.POLICY["--source glp-scout"] == (16, 13)


@pytest.mark.parametrize("case", ["foreign_session", "foreign_profile", "foreign_home", "foreign_cwd", "wrong_backend", "wrong_node", "missing_launcher", "launcher_outside_scope", "worker_prompt"])
def test_other_owners_remain_default(tmp_path, monkeypatch, case):
    module, proc, scope = fixture_owner(tmp_path, monkeypatch)
    if case in ("foreign_session", "foreign_profile"):
        p = proc / "100/cmdline"
        p.write_bytes(p.read_bytes().replace(SESSION.encode() if case == "foreign_session" else b"default", b"other"))
    elif case == "foreign_home":
        (proc / "102/environ").write_bytes(b"HERMES_HOME=/home/ubuntu/.hermes/profiles/other\0")
    elif case == "foreign_cwd":
        p = proc / "102/cwd"
        p.unlink(); p.symlink_to("/home/ubuntu/other")
    elif case == "wrong_backend":
        (proc / "102/cmdline").write_bytes(b"python\0-m\0tui_gateway.entry\0")
    elif case == "wrong_node":
        (proc / "101/cmdline").write_bytes(b"node\0other.js\0")
    elif case == "missing_launcher":
        (proc / "100/stat").unlink()
    elif case == "launcher_outside_scope":
        (scope / "cgroup.procs").write_text("101\n102\n")
    else:
        (proc / "102/cmdline").write_bytes(b"python\0worker.py\0prompt: tui_gateway.entry default " + SESSION.encode() + b"\0")
        (scope / "cgroup.procs").write_text("102\n")
    assert module.scope_owner(scope) == ("", "unclassified")
    assert module.DEFAULT == (12, 10)


@pytest.mark.parametrize("marker, expected", [("--source glp-scout", (16, 13)), ("--source kael-sol", (12, 10)), ("claude --resume", (20, 17))])
def test_existing_named_policy_unchanged(tmp_path, monkeypatch, marker, expected):
    module, proc, scope = fixture_owner(tmp_path, monkeypatch)
    (proc / "102/cmdline").write_bytes(marker.encode())
    assert module.scope_owner(scope)[0] == marker
    assert module.POLICY[marker] == expected


@pytest.mark.parametrize("have_max,have_high,anon,expected", [(12, 10, 3, [("memory.max", 16), ("memory.high", 13)]), (20, 17, 3, [("memory.high", 13), ("memory.max", 16)]), (12, 10, 17, []), (16, 13, 3, [])])
def test_guardian_preserves_write_order_and_oom_refusal(tmp_path, monkeypatch, have_max, have_high, anon, expected):
    module, _, scope = fixture_owner(tmp_path, monkeypatch)
    for name, value in {"memory.max": have_max, "memory.high": have_high, "memory.current": 19}.items():
        (scope / name).write_text(str(value * GIB))
    (scope / "memory.stat").write_text(f"anon {anon * GIB}\nfile {2 * GIB}\n")
    monkeypatch.setattr(module, "CGROOT", tmp_path)
    monkeypatch.setattr(module, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(module, "scope_owner", lambda scope: ("--source glp-scout", "glp-scout"))
    writes = []
    def write_limit(path, value):
        writes.append((path.name, value // GIB))
        path.write_text(str(value))
        assert int((scope / "memory.high").read_text()) < int((scope / "memory.max").read_text())
        return True
    monkeypatch.setattr(module, "write_limit", write_limit)
    assert module.main() == 0
    assert writes == expected
    assert module.anon_bytes(scope) == anon * GIB
    assert all(high < maximum for maximum, high in [*module.POLICY.values(), module.DEFAULT])


def protection_fixture(tmp_path, monkeypatch):
    module, proc, scope = fixture_owner(tmp_path, monkeypatch)
    root = tmp_path / "user.slice/user-1000.slice/user@1000.service"
    root.mkdir(parents=True)
    scope = scope.rename(root / scope.name)
    chain = [root.parent.parent, root.parent, root, scope]
    for group in chain:
        (group / "memory.low").write_text("0\n")
    monkeypatch.setattr(module, "CGROOT", root)
    writes = []
    def write(path, value):
        assert path.name == "memory.low"
        writes.append(path)
        path.write_text(str(value))
        return True
    monkeypatch.setattr(module, "write_limit", write)
    return module, proc, scope, chain, writes


def test_authenticated_cold_tui_gets_bounded_hierarchical_reclaim_protection(tmp_path, monkeypatch):
    module, _, scope, chain, writes = protection_fixture(tmp_path, monkeypatch)
    # Reproduce the measured zero-low hierarchy: a leaf-only low would be
    # ineffective against global reclaim with unprotected ancestors.
    getattr(module, "protect_interactive_memory", lambda scopes: [])([scope])
    assert [int((group / "memory.low").read_text()) for group in chain] == [8 * GIB] * 4
    assert writes == [group / "memory.low" for group in chain]
    assert module.POLICY["--source glp-scout"] == (16, 13)
    writes.clear()
    module.protect_interactive_memory([scope])
    assert writes == []  # natural repeats are idempotent, no restart/warmup


@pytest.mark.parametrize("case", ["foreign_profile", "prompt_marker", "missing_low", "malformed_low", "existing_larger_low", "duplicate_owner", "write_failure", "false_write_readback", "owner_disappears"])
def test_reclaim_protection_refuses_unproven_or_conflicting_policy(tmp_path, monkeypatch, case):
    module, proc, scope, chain, writes = protection_fixture(tmp_path, monkeypatch)
    if case == "foreign_profile":
        p = proc / "100/cmdline"
        p.write_bytes(p.read_bytes().replace(b"default", b"other"))
    elif case == "prompt_marker":
        (proc / "102/cmdline").write_bytes(b"worker\0--source glp-scout\0")
    elif case == "missing_low":
        (chain[0] / "memory.low").unlink()
    elif case == "malformed_low":
        (chain[0] / "memory.low").write_text("not a budget")
    elif case == "existing_larger_low":
        (chain[0] / "memory.low").write_text(str(9 * GIB))
    elif case == "duplicate_owner":
        import shutil
        shutil.copytree(scope, scope.with_name("tmux-spawn-duplicate.scope"))
    elif case in ("write_failure", "false_write_readback"):
        monkeypatch.setattr(module, "write_limit", lambda path, value: case != "write_failure")
    else:
        original = module.business_coordinator
        calls = [0]
        def disappearing(pid, pids):
            if calls[0]:
                return False
            found = original(pid, pids)
            if found:
                calls[0] = 1
            return found
        # Bind selection, then disappear before the first write.
        monkeypatch.setattr(module, "business_coordinator", disappearing)
    scopes = sorted(module.CGROOT.glob("tmux-spawn-*.scope"))
    module.protect_interactive_memory(scopes)
    assert writes == []
    assert (scope / "memory.low").read_text() == "0\n"


def test_main_restores_soft_preference_without_touching_existing_ceilings(tmp_path, monkeypatch):
    module, _, scope, chain, writes = protection_fixture(tmp_path, monkeypatch)
    for name, value in {"memory.max": 16, "memory.high": 13, "memory.current": 6}.items():
        (scope / name).write_text(str(value * GIB))
    (scope / "memory.stat").write_text(f"anon {2 * GIB}\n")
    monkeypatch.setattr(module, "STATE", tmp_path / "state.json")
    assert module.main() == 0
    assert writes == [group / "memory.low" for group in chain]
    assert int((scope / "memory.max").read_text()) == 16 * GIB
    assert int((scope / "memory.high").read_text()) == 13 * GIB
