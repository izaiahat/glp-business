"""Actual C orchestration, effects denied: stale indexes must not poison a pass."""
import json
from test_glp110_archive_admission import owner, execute, ep


def cohort(owner, monkeypatch):
    second = owner.root.parent / 'second-candidate'
    second.mkdir()
    calls = []
    def discovery(*args, progress, **kwargs):
        progress(owner.root.parent, 2)
        owner.source.write_text(json.dumps({'current_root': str(second)}))
        return [owner.root, second]
    def archive(api, root, pins, **kwargs):
        calls.append(root)
        held = pins.references(root)
        status = ('GATED_REFERENCE_CHANGED' if hasattr(pins, 'last_changed_source')
                  else 'GATED_REFERENCED' if held else 'GATED_YOUNG_FILE')
        return dict(rule='C_cold_evidence_archive', path=str(root), status=status,
                    ok=True, bytes_freed=0)
    monkeypatch.setattr(ep, 'archive_candidates', discovery)
    monkeypatch.setattr(ep, 'archive_tree', archive)
    return second, calls


def test_fresh_complete_index_protects_new_hold_without_replaying_first(owner, monkeypatch):
    second, calls = cohort(owner, monkeypatch)
    rows, candidates = execute(owner)
    assert [r['status'] for r in candidates] == ['GATED_REFERENCE_CHANGED', 'GATED_REFERENCED']
    assert calls == [owner.root, second]
    assert rows[-1]['partial'] is True
    assert (owner.root / 'evidence.txt').exists()


def test_refresh_failure_stops_remaining_candidates(owner, monkeypatch):
    second, calls = cohort(owner, monkeypatch)
    reads = 0
    def references(*args):
        nonlocal reads
        reads += 1
        if reads > 1:
            raise ValueError('required_reference_missing')
        return [owner.source]
    monkeypatch.setattr(ep, 'reference_files', references)
    rows, candidates = execute(owner)
    assert calls == [owner.root]
    assert any(r['status'] == 'GATED_REFERENCE_INVENTORY_INCOMPLETE'
               for r in rows if 'status' in r)
    assert owner.state['full_pass_complete'] is False
    assert second.exists()
