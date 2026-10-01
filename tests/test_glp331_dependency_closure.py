"""Narrow dependency regressions. Native full graph probes stay report-local.

No operational module imports. F1 compiles exact selected worker definitions;
F2 invokes the real refresher only against a disposable mismatched root, never
an operational root. Fixture strings are structural, not approval receipts.
"""
import ast
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import typing

import pytest

ROOT = Path(__file__).resolve().parents[1]


def support_namespace():
    text = (ROOT / 'scripts/glp_affiliate_wire_backlog_worker.py').read_text()
    names = {'B2Error', '_ensure_go_runtime_promotion_contract_support', '_detach'}
    constants = {'GO_PROMOTION_TEST_REPLACEMENTS', 'GO_PROMOTION_V2_TEST_MARKERS'}
    nodes = [n for n in ast.parse(text).body if getattr(n, 'name', '') in names or
             isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)]
    ns = {'Path': Path, 'Any': typing.Any, 'json': json, 'hashlib': hashlib, 're': re,
          'dt': dt, 'os': os, 'detach_for_write': lambda p: None}
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), '<exact-worker-support>', 'exec'), ns)
    return ns


def test_f1_current_v2_signature_and_unknown_code_refusal(tmp_path):
    ns = support_namespace()
    registry = tmp_path / 'src/program-registry.mjs'
    registry.parent.mkdir()
    # Exact maintained v2 marker contract and six-argument signature. This
    # portable structural fixture is separate from the report's exact-byte copy.
    registry.write_text('GO_RUNTIME_PROMOTION_AUTHORITY_V2\n'
                        'export function deriveGoEntriesFromRegistry(registry, profileId, ledgerPrograms = [], affiliateMap = {}, goPromotionAuthority = {}, commercialLabels = {}) {\n'
                        'goPromotionReceiptComplete(promotion, program, link)\n'
                        'candidate_gate: promotionActive ? null : (promotion?.reason || "go_runtime_promotion_authority_missing")\n}\n')
    for rel in ['scripts/regenerate-go-runtime-manifest.mjs', 'scripts/build-staging.mjs']:
        p = tmp_path / rel
        p.parent.mkdir(exist_ok=True)
        p.write_text('GO_RUNTIME_PROMOTION_AUTHORITY_V1\n')
    for rel, entry in ns['GO_PROMOTION_TEST_REPLACEMENTS'].items():
        p = tmp_path / rel.split('#')[0]
        p.parent.mkdir(exist_ok=True)
        with p.open('a') as handle:
            handle.write((entry[2] if len(entry) > 2 else entry[1]) + '\n')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = ns['_ensure_go_runtime_promotion_contract_support'](tmp_path)
    assert result['status'] == 'PASS_SUPPORTED'
    assert result['changed_files'] == []
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    registry.write_text(registry.read_text().replace('commercialLabels = {}) {', 'unreviewed = {}) {'))
    refused = registry.read_bytes()
    with pytest.raises(ns['B2Error'], match='go_runtime_promotion_registry_source_drift'):
        ns['_ensure_go_runtime_promotion_contract_support'](tmp_path)
    assert registry.read_bytes() == refused


def test_f2_real_refresher_refuses_mismatched_contract_before_import(tmp_path):
    source = (ROOT / 'scripts/glp_go_runtime_manifest_refresh.mjs').read_text()
    contract = json.loads(re.search(r'GO_REFRESH_CONTRACT = Object.freeze\((\{.*?\})\);', source, re.S)[1])
    metadata = json.loads((ROOT / 'ops/rebuild-source-manifest.json').read_text())['files']['scripts/glp_go_runtime_manifest_refresh.mjs']['runtime_contract']
    assert hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == metadata['code_and_selection_map_sha256']
    assert contract['src/go-runtime-selection.mjs'] == metadata['selection_module_sha256']
    assert len(contract) == metadata['source_module_count'] + metadata['selection_input_count']
    for rel in ['data/program-registry.json', 'data/program-content-ledger.json',
                'artifacts/ops/worker-manifest.staging.json', 'functions/go/program-manifest.js']:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{}')
    # An import sentinel makes the order observable: the predecessor reaches
    # candidate code before validating custody, the successor must never do so.
    for name in ['program-registry', 'go-runtime-manifest', 'affiliate-link-map', 'go-runtime-selection']:
        p = tmp_path / 'src' / (name + '.mjs')
        p.parent.mkdir(exist_ok=True)
        p.write_text('throw new Error("UNSAFE_IMPORT_SENTINEL");\n')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = subprocess.run(['node', str(ROOT / 'scripts/glp_go_runtime_manifest_refresh.mjs'),
                             '--release', str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert 'go_runtime_refresh_contract_drift' in result.stderr
    assert 'UNSAFE_IMPORT_SENTINEL' not in result.stderr
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
