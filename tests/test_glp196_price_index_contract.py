"""Portable GLP-196 contract tests; no private registry or provider activation.

Read the registered refresh function's literal migration table and exercise its
real replace_contract/COW writer. This is migration coverage, not a site build.
"""
import ast
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CURRENT = """  const frozenManifest=JSON.parse(await readFile(new URL('../artifacts/ops/price-index-manifest.json',import.meta.url)));
  assert.equal(result.live_provider_count,frozenManifest.live_provider_count);
"""


@pytest.fixture
def migration(monkeypatch):
    # Pin every imported local implementation to this checkout, not the host's
    # absolute scripts path inserted by the production entry point.
    old_path = sys.path[:]
    modules = {}
    try:
        for name in ("glp_served_test_contract", "site_tree_materializer",
                     "glp_provider_cardinality_test_refresh"):
            source = ROOT / "scripts" / f"{name}.py"
            spec = importlib.util.spec_from_file_location(name, source)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            monkeypatch.setitem(sys.modules, name, module)
            spec.loader.exec_module(module)
            assert Path(module.__file__).resolve() == source.resolve()
            modules[name] = module
        module = modules["glp_provider_cardinality_test_refresh"]
        tree = ast.parse(Path(module.__file__).read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "refresh")
        assignment = next(n for n in function.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "migrations" for t in n.targets))
        old, new, marker = ast.literal_eval(assignment.value)["price-index.test.mjs"]
        yield module, (old, new, marker)
    finally:
        sys.path[:] = old_path


def test_current_frozen_manifest_contract_is_retained_byte_for_byte(migration, tmp_path):
    module, contract = migration
    path = tmp_path / "price-index.test.mjs"
    path.write_text(CURRENT)
    before = path.stat()
    for _ in range(2):
        assert module.replace_contract(path, *contract) is False
        assert path.read_text() == CURRENT
        assert path.stat().st_ino == before.st_ino
        assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_legacy_literal_migrates_once_with_real_hardlink_detachment(migration, tmp_path):
    module, contract = migration
    old, new, _ = contract
    predecessor = tmp_path / "predecessor.mjs"
    predecessor.write_text(old + "\n")
    path = tmp_path / "price-index.test.mjs"
    path.hardlink_to(predecessor)
    assert module.replace_contract(path, *contract) is True
    assert path.read_text() == new + "\n"
    assert predecessor.read_text() == old + "\n"
    assert path.stat().st_ino != predecessor.stat().st_ino
    assert module.replace_contract(path, *contract) is False


@pytest.mark.parametrize("text", [
    "  assert.equal(result.live_provider_count, 99);\n",
    CURRENT.splitlines()[0] + "\n",  # source alone is not an assertion
    CURRENT.splitlines()[1] + "\n",  # assertion alone lacks the source
    CURRENT.replace("price-index-manifest.json", "unrelated-manifest.json"),
    CURRENT.replace("frozenManifest.live_provider_count", "frozenManifest.indexed_provider_count"),
    CURRENT.replace("assert.equal", "assert.notEqual"),
])
def test_unknown_or_partially_bound_contract_refuses_without_write(migration, tmp_path, text):
    module, contract = migration
    path = tmp_path / "price-index.test.mjs"
    path.write_text(text)
    before = path.stat()
    with pytest.raises(module.RefreshError, match="^provider_cardinality_contract_unknown:"):
        module.replace_contract(path, *contract)
    assert path.read_text() == text
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns
