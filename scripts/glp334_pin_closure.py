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
REPORTING_WRAPPERS = ['ops/hermes-cron-scripts/glp_affiliate_weekly_reporting'+s+'.sh'
                      for s in ('', '_j5', '_j6', '_j7')]
CONSUMER_WRAPPER = 'ops/hermes-cron-scripts/glp334_live_reconciliation.sh'
INTAKE_PAIRS = [('xentraffic-intake', 'xentraffic-sha256'),
                ('eden-intake', 'eden-intake-sha256'),
                ('statement-intake', 'statement-sha256')]
SHA256 = r'[a-f0-9]{64}'


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

    def refuse(path, owner, reason):
        drifts.append({'path': path, 'owner': owner, 'reason': reason,
                       'selected': path in chosen})

    def relative(path):
        return str(Path(path).relative_to(CANONICAL))

    def read(path, owner):
        if Path(path).is_absolute() or '..' in Path(path).parts or path in ('', '.'):
            refuse(path, owner, 'unsafe_pin_path')
            return None
        try:
            return ((checkout if path in chosen else installed)/path).read_bytes()
        except OSError:
            refuse(path, owner, 'missing_or_unreadable_file')
            return None

    def check(path, expected, owner):
        if not isinstance(expected, str) or not re.fullmatch(SHA256, expected):
            refuse(path, owner, 'malformed_sha256')
            return
        raw = read(path, owner)
        if raw is None:
            return
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            drifts.append({'path': path, 'owner': owner, 'expected': expected,
                           'actual': actual, 'selected': path in chosen})

    for manifest in MANIFESTS:
        raw = read(manifest, manifest)
        if raw is None:
            continue
        try:
            files = json.loads(raw)['files']
            if not isinstance(files, dict) or not files:
                raise ValueError('missing_files')
            for path, expected in files.items():
                check(relative(path), expected, manifest)
        except (ValueError, KeyError, TypeError):
            refuse(manifest, manifest, 'malformed_manifest_pins')
    raw = read(CONSUMER, CONSUMER)
    if raw is not None:
        try:
            tree = ast.parse(raw)
            declarations = [ast.literal_eval(node.value) for node in tree.body
                            if isinstance(node, ast.Assign) and any(
                                isinstance(t, ast.Name) and t.id == 'DEPENDENCIES'
                                for t in node.targets)]
            if len(declarations) != 1 or not isinstance(declarations[0], dict) or not declarations[0]:
                raise ValueError('missing_dependencies')
            for name, expected in declarations[0].items():
                check('scripts/'+name+'.py', expected, CONSUMER)
        except (ValueError, SyntaxError, TypeError):
            refuse(CONSUMER, CONSUMER, 'malformed_consumer_pins')

    # Fixed expected wrappers, not a possibly incomplete list of parsed matches.
    for wrapper in REPORTING_WRAPPERS+[CONSUMER_WRAPPER]:
        raw = read(wrapper, wrapper)
        if raw is None:
            continue
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            refuse(wrapper, wrapper, 'malformed_wrapper_encoding')
            continue
        if not text.strip():
            refuse(wrapper, wrapper, 'empty_wrapper')
            continue
        if wrapper == CONSUMER_WRAPPER:
            paths = re.findall(r'^source_path=(.*)$', text, re.M)
            pins = re.findall(r'^\[\[ "\$\{actual%% \*\}" == (\S+) \]\]', text, re.M)
            if paths != [CONSUMER] or len(pins) != 1 or not re.fullmatch(SHA256, pins[0]):
                refuse(wrapper, wrapper, 'missing_or_malformed_consumer_pin')
            else:
                check(CONSUMER, pins[0], wrapper)
            continue

        index = REPORTING_WRAPPERS.index(wrapper)
        manifest = MANIFESTS[index]
        job = 'scripts/'+Path(wrapper).stem+'_job.py'
        required = {job, manifest, 'ops/glp/reporting-v2/identity-registry.json',
                    'reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json',
                    'reports/ops/f12-host-followthrough-20261002/reconciliation-input.json'}
        pins = {}
        for line in re.findall(r'^check_source(?:[ \t].*)?$', text, re.M):
            match = re.fullmatch(r'check_source (\S+) ('+SHA256+r')', line)
            if not match:
                refuse(wrapper, wrapper, 'malformed_check_source_pin')
                continue
            path, expected = match.groups()
            if path in pins:
                refuse(wrapper, wrapper, 'duplicate_check_source_pin')
            pins[path] = expected
            check(path, expected, wrapper)
        if not required.issubset(pins):
            refuse(wrapper, wrapper, 'missing_check_source_pin')

        def argument(flag):
            # Only literal one-token declarations are supported, never shell eval.
            occurrences = re.findall(r'(?<![\w-])--'+re.escape(flag)+r'(?=[\s=)]|$)', text)
            values = re.findall(r'(?<![\w-])--'+re.escape(flag)+r'[ \t]+([^\s()]+)', text)
            if len(occurrences) != 1 or len(values) != 1:
                raise ValueError('missing_or_duplicate_argument')
            return values[0]

        for flag, hashflag in [('source-manifest', 'manifest-sha256')]+(INTAKE_PAIRS if index == 3 else []):
            try:
                path, expected = relative(argument(flag)), argument(hashflag)
                if flag == 'source-manifest' and path != manifest:
                    raise ValueError('wrong_manifest_path')
                if not re.fullmatch(SHA256, expected):
                    raise ValueError('malformed_sha256')
            except ValueError:
                refuse(wrapper, wrapper+' '+flag, 'missing_or_malformed_argument_pin')
                continue
            check(path, expected, wrapper+' '+flag)
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
