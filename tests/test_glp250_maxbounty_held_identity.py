"""GLP-250: only the exact held BistroMD row may gain its campaign Apply URL."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import affiliate_provider_metadata as m
import affiliate_sheet_batch_writer as writer

URL = 'https://affiliates.maxbounty.com/campaign/25152'
EVENT = 'evt_f278de1567bb0dbcf8c04a7c7bd0'


def fixture(tmp_path, *, row_number=800, program_id='25152', status='hold_sister_site_not_live',
            link='', category='Affiliate program', capture_event=EVENT):
    def pin(name, value):
        path = tmp_path / 'reports' / (name + '.json')
        path.parent.mkdir(exist_ok=True)
        raw = (json.dumps(value, sort_keys=True) + '\n').encode()
        path.write_bytes(raw)
        return str(path.relative_to(tmp_path)), hashlib.sha256(raw).hexdigest()

    source = [''] * len(m.EXPECTED_HEADERS)
    for name, value in {
        'Program / Network': 'BistroMD - Meal Delivery - CPS (US)',
        'Network / Route': 'MaxBounty', 'Official URL': 'https://www.bistromd.com/?',
        'Category': category, 'Status': status, 'Vertical': 'womens-health',
        'Wired Live?': 'FALSE', 'Tracking Link / Deep Link': link,
    }.items():
        source[m.COL[name]] = value
    capture_path = 'reports/capture.json'
    source[m.COL['Notes / Blocker']] = f'Portal capture={capture_path}. status=hold_sister_site_not_live.'
    capture_path, capture_sha = pin('capture', {
        'schema': 'glp.maxbounty.targeted-approval-capture.v1', 'sheet_row': row_number,
        'event_id': capture_event, 'programs': [{
            'program_id': program_id, 'name': source[m.COL['Program / Network']],
            'official_url': source[m.COL['Official URL']], 'detail_url': URL,
        }], 'campaign_detail_state': {
            'program_id': program_id, 'detail_url': URL, 'campaign_api_status': 200,
            'campaign_api_relationship': {'affiliate_campaign_status': 'Approved'},
        },
    })
    authority_path, authority_sha = pin('authority', {
        'task': 'GLP-250', 'sheet_row': row_number, 'network': 'MaxBounty',
        'network_program_id': program_id, 'apply_url': URL, 'category': category,
        'source_row_sha256': m.digest(source),
        'authority_type': 'explicit_operator_identity_category_repair',
    })
    manifest = {
        'schema': 'glp.provider_metadata.v1', 'kind': m.MAXBOUNTY_IDENTITY_KIND,
        'task': 'GLP-250', 'headers': list(m.EXPECTED_HEADERS),
        'sheet_row': row_number, 'source_row': source,
        'network_program_id': program_id, 'apply_url': URL, 'category': category,
        'authority_path': authority_path, 'authority_sha256': authority_sha,
        'capture_path': capture_path, 'capture_sha256': capture_sha,
    }
    manifest_path, manifest_sha = pin('manifest', manifest)
    entry = {'row': row_number, 'program': source[m.COL['Program / Network']],
             'cells': m.manifest_cells(m.MAXBOUNTY_IDENTITY_KIND, manifest),
             'evidence': {'metadata_manifest_path': manifest_path,
                          'metadata_manifest_sha256': manifest_sha}}
    values = [list(m.EXPECTED_HEADERS)] + [[] for _ in range(row_number - 2)] + [source]
    return entry, values, source


def test_exact_held_identity_has_only_apply_url_effect_and_cas_replay(tmp_path):
    entry, values, source = fixture(tmp_path)
    assert writer.SHEET_SYNC_MODES[m.MAXBOUNTY_IDENTITY_KIND].mutable_columns == (3, 5)
    assert entry['cells'] == {'3': 'Affiliate program', '5': URL}
    m.validate_entry(m.MAXBOUNTY_IDENTITY_KIND, entry, tmp_path, values)
    post = list(source)
    post[m.COL['Apply URL']] = URL
    values[799] = post
    m.validate_entry(m.MAXBOUNTY_IDENTITY_KIND, entry, tmp_path, values)
    post[m.COL['Status']] = 'approved_active_not_wired'
    with pytest.raises(RuntimeError, match='source_drift'):
        m.validate_entry(m.MAXBOUNTY_IDENTITY_KIND, entry, tmp_path, values)


@pytest.mark.parametrize('change', [
    {'row_number': 801}, {'program_id': '25153'}, {'status': 'hold_out_of_scope'},
    {'link': 'https://example.test/not-a-real-tracking-link'},
    {'category': ''}, {'capture_event': 'evt_other'},
])
def test_held_exception_refuses_other_identity_or_broken_hold(tmp_path, change):
    entry, values, _ = fixture(tmp_path, **change)
    with pytest.raises(RuntimeError):
        m.validate_entry(m.MAXBOUNTY_IDENTITY_KIND, entry, tmp_path, values)
