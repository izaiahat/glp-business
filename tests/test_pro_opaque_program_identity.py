"""Offline Pro regression for exact, network-scoped opaque program IDs."""
import json
from pathlib import Path
from types import SimpleNamespace


def test_pro_f6_all_dedupe_legs_preserve_opaque_identity(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    from scripts import glp_application_dedupe as dedupe
    from scripts import glp_application_machine as machine
    for program_id, distinct in [('Ab-C_007', 'ab-c_007'), ('Ab-C_007', 'AbC007'), ('00123', '123')]:
        row = {'name': 'North Orchard', 'network': 'impact', 'program_id': program_id,
               'status': 'approved', 'sheet_row': 2}
        ledger = tmp_path / 'ledger.json'
        ledger.write_text(json.dumps({'applications': [row]}))
        suppression = tmp_path / 'suppression.json'
        suppression.write_text(json.dumps({'entries': {'fixture': {
            'network': 'impact', 'campaign_id': program_id, 'advertiser_name': row['name']}}}))
        queue = tmp_path / 'queue.json'
        queue.write_text(json.dumps({'entries': {'impact:' + program_id: {'name': row['name']}}}))
        sheet_row = [''] * 27
        for column, value in ((machine.COL_PROGRAM, row['name']), (machine.COL_NETWORK, 'impact'),
                              (machine.COL_PROGRAM_ID, program_id), (machine.COL_STATUS, 'approved')):
            sheet_row[column] = value
        sheet = machine.sheet_index([['header'] * 27, sheet_row])
        assert ('impact', program_id) in sheet['engaged_ids']
        mirror = tmp_path / 'mirror.json'
        dedupe.write_sheet_mirror(sheet['engaged_names'], sheet['engaged_ids'], row_count=1,
                                  engaged_rows=sheet['engaged_rows'], path=mirror)
        fake_site = SimpleNamespace(release_root=lambda: tmp_path,
            go_manifest_programs=lambda _: {'programs': {'orchard': {}}},
            registry_live_programs=lambda _: {'orchard': {'display_name': row['name']}},
            identity_map=lambda _: {'orchard': {'network': 'impact', 'network_program_id': program_id}})
        monkeypatch.setattr(dedupe, '_load_by_path', lambda *a: fake_site)
        legs = [dedupe.site_identities(), dedupe._sheet_leg(sheet, None, {}),
                dedupe._sheet_leg(None, mirror, {}), dedupe.ledger_identities(ledger),
                dedupe.suppression_identities(suppression), dedupe.queue_identities(queue),
                dedupe.queue_identities(tmp_path / 'missing', queue_rows=[{**row, 'program_name': row['name']}])]
        for leg in legs:
            assert ('impact', program_id) in leg['pairs']
            index = dedupe.DedupeIndex()
            index.add(dedupe.LEG_LEDGER, leg['names'], leg['pairs'], leg['leg'],
                      leg.get('domains'), leg.get('brands'))
            assert index.verdict('South Meadow', 'impact', program_id) is not None
            assert index.verdict('South Meadow', 'impact', distinct) is None
            assert index.verdict('South Meadow', 'cj', program_id) is None
        # Brand identity remains its own cross-network safeguard.
        assert index.verdict('North Orchard', 'cj', 'unrelated') is not None
