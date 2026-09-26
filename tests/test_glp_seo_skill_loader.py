"""Offline regression for the consolidated weekly SEO instruction loader."""
import hashlib
import importlib.util
from pathlib import Path


def test_loads_canonical_procedures_in_required_order_without_retired_skills(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "ops/glp/seo_business_context.py"
    spec = importlib.util.spec_from_file_location("weekly_seo_context_test", source)
    assert spec is not None and spec.loader is not None
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    monkeypatch.setattr(loader, "SKILL_ROOT", tmp_path)

    # Logical evidence keys stay compatible with seo_weekly_cogny.RULES.
    # These short fixture documents are not production skill/source evidence.
    required = [
        ("comparison-site-seo", "comparison-site-programmatic-seo/references/consolidated/comparison-site-seo/PROCEDURE.md"),
        ("comparison-site-programmatic-seo", "comparison-site-programmatic-seo/SKILL.md"),
        ("gsc-page-two-optimization", "gsc-page-two-optimization/SKILL.md"),
        ("intent-internal-linking", "intent-internal-linking/SKILL.md"),
        ("provider-review-seo", "provider-review-seo/SKILL.md"),
        ("search-console-bing-operations", "gsc-page-two-optimization/references/consolidated/search-console-bing-operations/PROCEDURE.md"),
    ]
    for name, relative in required:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Offline instruction fixture: {name}\n", encoding="utf-8")
    for retired in ("comparison-site-seo", "search-console-bing-operations"):
        assert not (tmp_path / retired).exists()

    loaded = loader.load_seo_skills()

    assert list(loaded) == [name for name, _ in required]
    for name, relative in required:
        path = tmp_path / relative
        raw = path.read_bytes()
        assert loaded[name] == {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "text": raw.decode("utf-8"),
        }
