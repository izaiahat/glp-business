"""seat_continuation: capacity-aware rearm (H9) and briefs that actually pass the gates (H10)."""
import json, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import seat_continuation as SC
import seat_autodispatch as SA
import brief_lint, brief_clause_gate as BCG


def _capacity(tmp_path, status):
    p = tmp_path / 'capacity.json'
    p.write_text(json.dumps({"collected_at_utc": "2099-01-01T00:00:00Z", "records": [
        {"provider": "openai-codex", "label": f"c{i}", "capacity_status": status,
         "weekly_reset_at_utc": "2026-09-19T08:12:51Z"} for i in range(3)]}))
    return p


def test_depleted_provider_holds_the_seat_before_any_probe(tmp_path, monkeypatch):
    """Deleting this hold left the whole selection green (review 2026-09-16, M16)."""
    monkeypatch.setattr(SA, 'CAPACITY', _capacity(tmp_path, 'depleted'))
    reg = tmp_path / 'register.json'
    reg.write_text(json.dumps({"rows": [{"id": "D1", "matrix_status": "UNMET", "matrix_owner": "glp-scout"}]}))
    standing = tmp_path / 'standing.json'
    standing.write_text(json.dumps({"seats": {"glp-scout": {"brief": None, "rearm_probe": "true"}}}))
    monkeypatch.setattr(SC, 'REGISTER', reg)
    monkeypatch.setattr(SC, 'STANDING', standing)
    monkeypatch.setattr(SC, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(SC, 'probe', lambda *a, **k: pytest.fail('probe must not run on a depleted provider'))
    monkeypatch.setattr(sys, 'argv', ['seat_continuation.py', '--seats', 'glp-scout'])
    monkeypatch.setattr(SC, 'OUTDIR', tmp_path / 'out')
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        SC.main()
    report = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert report['held'] and 'depleted' in report['held'][0]['reason']


def test_composed_brief_passes_brief_lint_and_the_clause_gate(tmp_path, monkeypatch):
    """H10: the consumer's own output was refused by the gate it feeds."""
    body = SC.compose('glp-scout', ['D35', 'W13'])
    b = tmp_path / 'glp-scout.md'; b.write_text(body)
    assert brief_lint.lint(b) == [], brief_lint.lint(b)
    monkeypatch.setattr(BCG, 'fable_obligation_rows', lambda *a, **k: [])
    code, rep = BCG.check(b, 'GLP-38', 'glp-scout')
    assert code == 0 and rep.get('enabling_work'), rep
