import hashlib
import json
import subprocess
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
