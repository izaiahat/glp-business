"""Active TUI without child leases must still defer; all probes are offline."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_coordinator_guard as m


def test_active_tui_without_native_children_defers(monkeypatch):
    monkeypatch.setattr(m, 'inspect', lambda **kw: {'status': 'IDLE'})
    state = m.inspect_idle_tui(census=lambda: {'unreadable_source_count': 0, 'tui_census': {
        'tuis': [{'state': 'ACTIVE', 'signals': {'pane_activity_state': 'active'}}]}})
    assert state['status'] == 'DEFERRED_TUI_ACTIVE'


def test_idle_is_positive_evidence_unknown_pane_or_unreadable_defers(monkeypatch):
    monkeypatch.setattr(m, 'inspect', lambda **kw: {'status': 'IDLE'})
    for pane, unreadable, expected in [('idle', 0, 'IDLE'), ('no_pane', 0, 'DEFERRED_UNKNOWN'),
                                       ('capture_failed', 0, 'DEFERRED_UNKNOWN'), ('idle', 1, 'DEFERRED_UNKNOWN')]:
        assert m.inspect_idle_tui(census=lambda: {'unreadable_source_count': unreadable, 'tui_census': {
            'tuis': [{'state': 'IDLE', 'signals': {'pane_activity_state': pane}}]}})['status'] == expected
