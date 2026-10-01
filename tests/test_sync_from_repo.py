import hashlib
import json
import shutil
import stat
import subprocess
import sys
import pytest
from pathlib import Path
from scripts import sync_from_repo
from scripts.sync_from_repo import sync

def digest(data):
    return hashlib.sha256(data).hexdigest()

def checkout(tmp_path):
    repo = tmp_path / "repo"
    root = tmp_path / "live"
    repo.mkdir(); root.mkdir(); (repo / "scripts").mkdir(); (repo / "ops").mkdir()
    (repo / "scripts/sample.py").write_bytes(b"old\n")
    m = {"schema": "GLP_BUSINESS_SOURCE_V1", "tracked_count": 2,
         "files": {"scripts/sample.py": {"sha256": digest(b"old\n")}}}
    (repo / "ops/rebuild-source-manifest.json").write_text(json.dumps(m))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "scripts/sample.py", "ops/rebuild-source-manifest.json"], check=True)
    return repo, root

def test_reviewed_baseline_adoption_requires_historical_bytes(tmp_path, capsys):
    repo, root = checkout(tmp_path)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "original"], check=True)
    (repo / "scripts/sample.py").write_bytes(b"new\n")
    mpath = repo / "ops/rebuild-source-manifest.json"
    m = json.loads(mpath.read_text())
    m["files"]["scripts/sample.py"]["sha256"] = digest(b"new\n")
    mpath.write_text(json.dumps(m))
    subprocess.run(["git", "-C", str(repo), "add", "scripts/sample.py", str(mpath)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "reviewed update"], check=True)
    (root / "scripts").mkdir()
    local = root / "scripts/sample.py"
    local.write_bytes(b"old\n")
    assert sync(root, repo, False) == 2
    assert sync(root, repo, False, ["scripts/sample.py"]) == 0
    assert "DRY_RUN diffs=" in capsys.readouterr().out
    local.write_bytes(b"UNREVIEWED\n")
    assert sync(root, repo, True, ["scripts/sample.py"]) == 2
    assert local.read_bytes() == b"UNREVIEWED\n"
    local.write_bytes(b"old\n")
    assert sync(root, repo, True, ["scripts/sample.py"]) == 0
    assert local.read_bytes() == b"new\n"
    assert sync(root, repo, False) == 0
    assert "DRY_RUN diffs=0 conflicts=0" in capsys.readouterr().out
    with pytest.raises(ValueError, match="absent from sync state"):
        sync(root, repo, False, ["scripts/sample.py"])


def test_reviewed_baseline_adoption_from_treesame_side_branch(tmp_path):
    repo, root = checkout(tmp_path)

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c",
             "user.email=test@example.invalid", *args], text=True).strip()

    git("commit", "-qm", "original")
    git("branch", "first-parent")
    git("checkout", "-qb", "side")
    sample = repo / "scripts/sample.py"
    sample.write_bytes(b"side predecessor\n")
    git("commit", "-qam", "side predecessor")
    predecessor = git("rev-parse", "HEAD")
    sample.write_bytes(b"old\n")
    git("commit", "-qam", "return to original bytes")
    git("checkout", "-q", "first-parent")
    git("merge", "--no-ff", "-m", "TREESAME merge", "side")
    assert git("diff", "HEAD^1", "HEAD", "--", "scripts/sample.py") == ""
    assert predecessor not in git("log", "--format=%H", "HEAD", "--", "scripts/sample.py").splitlines()
    assert predecessor in git("log", "--full-history", "--format=%H", "HEAD", "--", "scripts/sample.py").splitlines()

    (root / "scripts").mkdir()
    local = root / "scripts/sample.py"
    local.write_bytes(b"side predecessor\n")
    assert sync(root, repo, False) == 2  # Explicit path authorization is required.
    assert sync(root, repo, False, ["scripts/sample.py"]) == 0
    assert local.read_bytes() == b"side predecessor\n"
    assert not (root / sync_from_repo.STATE).exists()

    local.write_bytes(b"never committed\n")
    assert sync(root, repo, False, ["scripts/sample.py"]) == 2
    assert local.read_bytes() == b"never committed\n"
    # HEAD's bytes occurred in older commits too: exclude by content, not position.
    hashes = sync_from_repo.reviewed_prior_hashes(repo, "scripts/sample.py")
    assert digest(b"old\n") not in hashes
    assert digest(b"side predecessor\n") in hashes
    sample.write_bytes(b"working tree only\n")
    assert sync_from_repo.reviewed_prior_hashes(repo, "scripts/sample.py") == hashes


def test_reviewed_baseline_adoption_rejects_untracked_or_duplicate_paths(tmp_path):
    repo, root = checkout(tmp_path)
    for paths in (["scripts/other.py"], ["scripts/sample.py", "scripts/sample.py"]):
        with pytest.raises(ValueError, match="unique curated path"):
            sync(root, repo, False, paths)


def test_initial_import_then_clean_dry_run(tmp_path, capsys):
    repo, root = checkout(tmp_path)
    assert sync(root, repo, True) == 0
    assert sync(root, repo, False) == 0
    assert "DRY_RUN diffs=0 conflicts=0" in capsys.readouterr().out
    assert (root / "scripts/sample.py").read_bytes() == b"old\n"

def test_local_edits_refused_before_any_write(tmp_path):
    repo, root = checkout(tmp_path)
    assert sync(root, repo, True) == 0
    (root / "scripts/sample.py").write_bytes(b"LOCAL\n")
    (repo / "scripts/sample.py").write_bytes(b"new\n")
    mpath = repo / "ops/rebuild-source-manifest.json"
    m = json.loads(mpath.read_text()); m["files"]["scripts/sample.py"]["sha256"] = digest(b"new\n")
    mpath.write_text(json.dumps(m))
    assert sync(root, repo, True) == 2
    assert (root / "scripts/sample.py").read_bytes() == b"LOCAL\n"

def test_clean_upstream_change_applies(tmp_path):
    repo, root = checkout(tmp_path)
    assert sync(root, repo, True) == 0
    (repo / "scripts/sample.py").write_bytes(b"new\n")
    mpath = repo / "ops/rebuild-source-manifest.json"
    m = json.loads(mpath.read_text()); m["files"]["scripts/sample.py"]["sha256"] = digest(b"new\n")
    mpath.write_text(json.dumps(m))
    assert sync(root, repo, False) == 0
    assert (root / "scripts/sample.py").read_bytes() == b"old\n"
    assert sync(root, repo, True) == 0
    assert (root / "scripts/sample.py").read_bytes() == b"new\n"


@pytest.mark.parametrize("destination_mode,source_mode", [(0o700, 0o755), (0o600, 0o644)])
def test_scoped_replacement_preserves_mode_and_refuses_mode_only_drift(
        tmp_path, monkeypatch, destination_mode, source_mode):
    repo, root = checkout(tmp_path)
    source = repo / "scripts/sample.py"
    source.chmod(source_mode)
    # A new destination still inherits checkout mode, including in a scoped import.
    assert sync(root, repo, True, only=["scripts/sample.py"]) == 0
    destination = root / "scripts/sample.py"
    assert stat.S_IMODE(destination.stat().st_mode) == source_mode

    destination.chmod(destination_mode)
    source.write_bytes(b"new\n")
    manifest_path = repo / "ops/rebuild-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["scripts/sample.py"]["sha256"] = digest(b"new\n")
    manifest_path.write_text(json.dumps(manifest))
    state_path = root / sync_from_repo.STATE
    state_before = state_path.read_bytes()
    real_fsync = sync_from_repo.os.fsync
    changed = [False]

    def mode_only_intervening_edit(fd):
        if not changed[0]:
            destination.chmod(source_mode)
            changed[0] = True
        return real_fsync(fd)

    with monkeypatch.context() as patcher:
        patcher.setattr(sync_from_repo.os, "fsync", mode_only_intervening_edit)
        with pytest.raises(ValueError, match="destination mode changed during sync"):
            sync(root, repo, True, only=["scripts/sample.py"])
    assert changed[0]
    assert destination.read_bytes() == b"old\n"
    assert stat.S_IMODE(destination.stat().st_mode) == source_mode
    assert state_path.read_bytes() == state_before

    destination.chmod(destination_mode)
    assert sync(root, repo, True, only=["scripts/sample.py"]) == 0
    assert destination.read_bytes() == b"new\n"
    assert stat.S_IMODE(destination.stat().st_mode) == destination_mode


def test_retirement_moves_matching_uncurated_source(tmp_path, capsys):
    repo, root = checkout(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts/old.py").write_bytes(b"retire\n")
    archive = repo / "scripts/.retired-20260925/old.py"
    archive.parent.mkdir()
    archive.write_bytes(b"retire\n")
    mpath = repo / "ops/rebuild-source-manifest.json"
    m = json.loads(mpath.read_text())
    m["files"]["scripts/.retired-20260925/old.py"] = {"sha256": digest(b"retire\n")}
    m["tracked_count"] += 1
    mpath.write_text(json.dumps(m))
    subprocess.run(["git", "-C", str(repo), "add", "-f", str(archive)], check=True)
    assert sync(root, repo, False) == 0
    assert (root / "scripts/old.py").read_bytes() == b"retire\n"
    assert sync(root, repo, True) == 0
    assert not (root / "scripts/old.py").exists()
    assert (root / "scripts/.retired-20260925/old.py").read_bytes() == b"retire\n"
    assert sync(root, repo, False) == 0
    assert "DRY_RUN diffs=0 conflicts=0" in capsys.readouterr().out


def test_retirement_registry_releases_dormant_producers():
    """A retired entry point must not remain a registered ordinary writer."""
    root = Path(__file__).resolve().parents[1]
    registry = json.loads((root / "ops/registered-producers.json").read_text())
    retired = {p for p in json.loads((root / "ops/rebuild-source-manifest.json").read_text())["files"]
               if p.startswith("scripts/.retired-20260925/")}
    retired_origins = {"scripts/" + Path(p).name for p in retired}
    callers = [(row["glob"], producer) for row in registry["artifacts"]
               for producer in row["producers"] if producer in retired_origins]
    assert callers == []


def test_protected_continuation_imports_retained_dispatcher_from_checkout(tmp_path):
    """The post-sync scripts layout, isolated from the host, resolves the pinned import."""
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "ops/rebuild-source-manifest.json").read_text())["files"]
    scripts = tmp_path / "live/scripts"
    scripts.mkdir(parents=True)
    for name in ("seat_continuation.py", "seat_autodispatch.py", "brief_model_effort.py"):
        rel = f"scripts/{name}"
        assert rel in manifest, f"missing live curated dependency: {name}"
        assert digest((root / rel).read_bytes()) == manifest[rel]["sha256"]
        shutil.copy2(root / rel, scripts / name)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "import seat_continuation, seat_autodispatch; "
            "assert seat_continuation._provider_depleted is seat_autodispatch.provider_depleted; "
            "assert seat_autodispatch.__file__ == sys.argv[1] + '/seat_autodispatch.py'")
    subprocess.run([sys.executable, "-I", "-c", code, str(scripts)],
                   cwd=tmp_path, check=True)


def test_intervening_conventions_edit_refused_before_replacement(tmp_path, monkeypatch):
    repo, root = checkout(tmp_path)
    (repo / "ops/linear").mkdir(parents=True)
    (repo / "ops/linear/CONVENTIONS.md").write_bytes(b"original\n")
    mpath = repo / "ops/rebuild-source-manifest.json"
    m = json.loads(mpath.read_text())
    m["files"]["ops/linear/CONVENTIONS.md"] = {"sha256": digest(b"original\n")}
    m["tracked_count"] += 1
    mpath.write_text(json.dumps(m))
    subprocess.run(["git", "-C", str(repo), "add", "ops/linear/CONVENTIONS.md"], check=True)
    assert sync(root, repo, True) == 0
    (repo / "ops/linear/CONVENTIONS.md").write_bytes(b"upstream\n")
    m["files"]["ops/linear/CONVENTIONS.md"]["sha256"] = digest(b"upstream\n")
    mpath.write_text(json.dumps(m))
    state_before = (root / "ops/.rebuild-sync-state.json").read_bytes()
    real_fsync = sync_from_repo.os.fsync
    destination = root / "ops/linear/CONVENTIONS.md"
    def intervening_fsync(fd):
        if not injected[0]:
            destination.write_bytes(b"PARALLEL EDIT\n")
            injected[0] = True
        return real_fsync(fd)
    injected = [False]
    monkeypatch.setattr(sync_from_repo.os, "fsync", intervening_fsync)
    # Injection after preflight but before replacement proves the destination survives.
    with pytest.raises(ValueError, match="destination changed during sync: ops/linear/CONVENTIONS.md"):
        sync(root, repo, True)
    assert injected[0]
    assert destination.read_bytes() == b"PARALLEL EDIT\n"
    assert (root / "ops/.rebuild-sync-state.json").read_bytes() == state_before


def test_retirement_refuses_modified_source_before_other_writes(tmp_path):
    repo, root = checkout(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts/old.py").write_bytes(b"LOCAL\n")
    archive = repo / "scripts/.retired-20260925/old.py"
    archive.parent.mkdir()
    archive.write_bytes(b"retire\n")
    mpath = repo / "ops/rebuild-source-manifest.json"
    m = json.loads(mpath.read_text())
    m["files"]["scripts/.retired-20260925/old.py"] = {"sha256": digest(b"retire\n")}
    m["tracked_count"] += 1
    mpath.write_text(json.dumps(m))
    subprocess.run(["git", "-C", str(repo), "add", "-f", str(archive)], check=True)
    assert sync(root, repo, True) == 2
    assert (root / "scripts/old.py").read_bytes() == b"LOCAL\n"
    assert not (root / "scripts/.retired-20260925/old.py").exists()
    assert not (root / "scripts/sample.py").exists()


def test_help_epilog_documents_dry_run_then_apply():
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, str(root / "scripts/sync_from_repo.py"), "--help"],
                         capture_output=True, text=True, check=True).stdout
    assert "--dry-run" in out.split("workflow:")[1]
    assert out.index("1. python3 scripts/sync_from_repo.py --dry-run") < out.index("2. python3 scripts/sync_from_repo.py --apply")


def test_real_committed_source_manifest_passes_sync_preflight(tmp_path, capsys):
    """CI must reject stale postimage pins in the actual checkout, not just fixtures."""
    repo = Path(__file__).resolve().parents[1]
    manifest = json.loads((repo / sync_from_repo.MANIFEST).read_text())
    files = manifest["files"]
    required = {".gitignore", "scripts/account_capacity_collector.py",
                "scripts/run_tests.sh", "tests/test_fable_bridge_native_review.py"}
    assert required <= files.keys()
    committed = set(subprocess.check_output(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD"], text=True
    ).splitlines())
    assert committed == files.keys() | {sync_from_repo.MANIFEST}
    assert manifest["tracked_count"] == len(committed)
    mismatches = {}
    for rel, info in files.items():
        committed_bytes = subprocess.check_output(
            ["git", "-C", str(repo), "show", f"HEAD:{rel}"]
        )
        actual = digest(committed_bytes)
        if info["sha256"] != actual:
            mismatches[rel] = {"manifest": info["sha256"], "committed": actual}
    assert mismatches == {}, f"committed source-manifest hash mismatches: {mismatches}"
    assert sync(tmp_path, repo, False) == 0
    assert "conflicts=0" in capsys.readouterr().out
