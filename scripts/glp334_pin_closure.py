#!/usr/bin/env python3
"""Read-only projection of the reviewed GLP334 cohort; never installs or runs jobs.

Source paths selected by the declaration overlay the installed root in memory.
All other dependencies are checked against installed bytes. No pins are rewritten.
A ready result is source compatibility, not receiving or live coverage acceptance.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

CANONICAL = Path('/home/ubuntu/business')
MANIFESTS = ['ops/glp/reporting-v2/source-manifest'+suffix+'.json'
             for suffix in ('', '-j5', '-j6', '-j7')]
CONSUMER = 'scripts/glp334_live_reconciliation.py'


def project(checkout, installed, cohort, selected=None):
    checkout, installed = Path(checkout), Path(installed)
    selected = cohort['selected_paths'] if selected is None else selected
    if len(selected) != len(set(selected)):
        raise ValueError('duplicate_selected_path')
    for path in selected:
        if Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('unsafe_selected_path')
    chosen = set(selected)
    drifts = []

    def read(path):
        try:
            return ((checkout if path in chosen else installed)/path).read_bytes()
        except FileNotFoundError:
            return b''

    def check(path, expected, owner):
        actual = hashlib.sha256(read(path)).hexdigest()
        if actual != expected:
            drifts.append({'path': path, 'owner': owner, 'expected': expected,
                           'actual': actual, 'selected': path in chosen})

    for manifest in MANIFESTS:
        for path, expected in json.loads(read(manifest))['files'].items():
            check(str(Path(path).relative_to(CANONICAL)), expected, manifest)
    tree = ast.parse(read(CONSUMER))
    dependencies = next(ast.literal_eval(node.value) for node in tree.body
                        if isinstance(node, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == 'DEPENDENCIES'
                            for t in node.targets))
    for name, expected in dependencies.items():
        check('scripts/'+name+'.py', expected, CONSUMER)
    wrappers = [p for p in cohort['selected_paths'] if p.endswith('.sh')]
    for wrapper in wrappers:
        text = read(wrapper).decode()
        for path, expected in re.findall(r'^check_source (\S+) ([a-f0-9]{64})$', text, re.M):
            check(path, expected, wrapper)
        manifest = re.findall(r'--source-manifest (\S+)', text)
        manifest_sha = re.findall(r'--manifest-sha256 ([a-f0-9]{64})', text)
        for path, expected in zip(manifest, manifest_sha, strict=True):
            check(str(Path(path).relative_to(CANONICAL)), expected, wrapper+' argv')
        if wrapper.endswith('/glp334_live_reconciliation.sh'):
            expected = re.search(r'== ([a-f0-9]{64})', text).group(1)
            check(CONSUMER, expected, wrapper)
    # The successor guard is intentional; never heal this by rolling back to 547f31d.
    successor = cohort['maintained_successor']
    check(successor['path'], successor['sha256'], 'reviewed_earnings_guard')
    return {'status': 'BLOCKED_PROJECTED_PINS' if drifts else 'READY_SOURCE_ONLY',
            'selected_paths': list(selected), 'drifts': drifts,
            'effects': 'NONE_READ_ONLY_PROJECTION_NOT_INSTALLATION'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--installed-root', type=Path, required=True)
    parser.add_argument('--selection', type=Path, help='Optional JSON path list, for reproducing an older selection')
    args = parser.parse_args()
    cohort = json.loads((args.checkout/'ops/glp/reporting-v2/glp334-install-cohort.json').read_text())
    selected = json.loads(args.selection.read_text()) if args.selection else None
    result = project(args.checkout, args.installed_root, cohort, selected)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result['drifts'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
