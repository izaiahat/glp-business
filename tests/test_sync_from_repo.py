import hashlib
import json
import subprocess
from pathlib import Path
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
