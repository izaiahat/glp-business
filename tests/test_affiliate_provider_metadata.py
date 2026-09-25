"""GLP-163 acceptance regressions; all evidence records are fixture only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import affiliate_provider_metadata as metadata


def test_provider_metadata_maxbounty_row815_program32639(tmp_path):
    source = [''] * len(metadata.EXPECTED_HEADERS)
    fields = {
        'Program / Network': 'Fixture Only Provider - CPS (US)',
        'Network / Route': 'MaxBounty',
        'Official URL': 'https://fixture-only.example/',
        'SEO Slug': 'fixture-only-provider-review',
        'Category': 'Affiliate program',
        'Status': 'applied_pending_brand_approval',
        'Wired Live?': 'FALSE',
        'Notes / Blocker': 'FIXTURE ONLY',
    }
    for header, value in fields.items():
        source[metadata.COL[header]] = value
    apply_url = 'https://affiliates.maxbounty.com/campaign/32639'
    category = 'glp1-weightloss'

    def bound_fixture(name, record):
        record = dict(record, proof_class='fixture_only')
        path = tmp_path / 'reports' / ('fixture-only-' + name + '.json')
        path.parent.mkdir(exist_ok=True)
        raw = json.dumps(record, sort_keys=True).encode()
        path.write_bytes(raw)
        return str(path.relative_to(tmp_path)), hashlib.sha256(raw).hexdigest()

    authority_path, authority_sha = bound_fixture('authority', {
        'task': 'GLP-163',
        'sheet_row': 815,
        'network': 'MaxBounty',
        'network_program_id': '32639',
        'apply_url': apply_url,
        'category': category,
        'source_row_sha256': metadata.digest(source),
        'authority_type': 'explicit_operator_identity_category_repair',
    })
    capture_path, capture_sha = bound_fixture('capture', {
        'program_id': '32639',
        'program': {
            'program_id': '32639',
            'name': fields['Program / Network'],
            'official_url': fields['Official URL'],
            'detail_url': apply_url,
        },
        'state': {
            'detail_url': apply_url,
            'campaign_api_status': 200,
            'campaign_api_relationship': {'affiliate_campaign_status': 'Approved'},
        },
    })
    manifest = {
        'schema': 'glp.provider_metadata.v1',
        'kind': metadata.MAXBOUNTY_IDENTITY_KIND,
        'task': 'GLP-163',
        'headers': list(metadata.EXPECTED_HEADERS),
        'sheet_row': 815,
        'source_row': source,
        'network_program_id': '32639',
        'apply_url': apply_url,
        'category': category,
        'authority_path': authority_path,
        'authority_sha256': authority_sha,
        'capture_path': capture_path,
        'capture_sha256': capture_sha,
    }
    manifest_path, manifest_sha = bound_fixture('manifest', manifest)
    cells = {
        str(metadata.COL['Category']): category,
        str(metadata.COL['Apply URL']): apply_url,
    }
    entry = {
        'row': 815,
        'program': fields['Program / Network'],
        'cells': cells,
        'evidence': {
            'metadata_manifest_path': manifest_path,
            'metadata_manifest_sha256': manifest_sha,
        },
    }
    values = [list(metadata.EXPECTED_HEADERS)] + [[] for _ in range(813)] + [source]
    assert metadata.validate_entry(metadata.MAXBOUNTY_IDENTITY_KIND, entry, tmp_path, values) is None
    assert metadata.manifest_cells(metadata.MAXBOUNTY_IDENTITY_KIND, manifest) == cells
    assert values[814] == source
    assert metadata.cell(source, 'Status') == 'applied_pending_brand_approval'
    assert metadata.cell(source, 'Wired Live?') == 'FALSE'


def test_provider_metadata_cli_help_exposes_row_program_identity():
    result = subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / 'affiliate_provider_metadata.py'), '--help'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    for flag in ('--row', '--program-id', '--manifest'):
        assert flag in result.stdout, result.stdout
