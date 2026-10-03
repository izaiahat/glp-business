#!/usr/bin/env python3
"""Dated first-party price evidence -> guarded provider-registry delta (F04).

Monthly branch only: claims about price components and cancellation terms are
validated against hash-bound captures, then projected into an exact JSON patch
for ``provider-pricing-matrix.json``. Every adopted fact carries source URL,
content hash and capture time. Stale, conflicting, unprovenanced or held
claims refuse the whole branch; no partial patch is produced. The output is an
intent for F06 acceptance, never a published Price Index, and nothing here
writes the candidate, deploys or makes network calls.
"""
import argparse
import copy
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evidence_refresh import apply_patch, diff, digest, pointer, put, read, safe_url, sha, stable  # noqa: E402

CLAIMS_SCHEMA = 'glp-price-evidence-claims-v1'
BRANCH_SCHEMA = 'glp-price-evidence-monthly-branch-v1'
INTENT_SCHEMA = 'glp-f06-publication-intent-v1'
PATCH_SCHEMA = 'ws-r-json-patch-v1'
# Mirrors the typed pricing witness bar (validate-provider-pricing-matrix.mjs).
MAX_EVIDENCE_AGE_DAYS = 92
PRICE_FIELDS = frozenset({'amount_usd', 'period', 'billing_basis', 'price_phase', 'price_kind',
                          'medication', 'formulation'})
CANCELLATION_FIELDS = frozenset({'cancellation_policy', 'notice_days', 'refund_policy',
                                 'minimum_commitment_months', 'cancellation_fee_usd'})
PROVENANCE = ('source_url', 'content_sha256', 'captured_at_utc')
PUBLICATION_KEYS = ('published', 'price_index_published', 'publication_claimed')
DISPOSITIONS = ('ACCEPTED', 'NO_CHANGE', 'REFUSED')


def parse_utc(value):
    if not isinstance(value, str) or not value:
        raise ValueError('timestamp missing')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('timestamp lacks timezone: ' + value)
    return moment.astimezone(timezone.utc)


def month_bounds(month):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month or ''):
        raise ValueError('month must be YYYY-MM')
    start = datetime.fromisoformat(month + '-01T00:00:00+00:00')
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def is_held(pid, provider_row, held_providers):
    if pid in held_providers or 'fella' in pid.lower():
        return True
    for key, value in (provider_row or {}).items():
        if re.search(r'hold|held', key, re.I) and value not in (None, False, '', []):
            return True
        if isinstance(value, str) and re.search(r'\bHELD\b|^HOLD', value):
            return True
    return False


def load_capture(key, cap, capture_root):
    """Return capture text or raise; unusable captures are refusals, not skips."""
    for field in PROVENANCE:
        if not cap.get(field):
            raise ValueError('capture provenance missing: %s.%s' % (key, field))
    if cap.get('status') != 'CAPTURED' or cap.get('forced_fresh') is not True:
        raise ValueError('capture not a fresh first-party body: ' + key)
    path = Path(cap.get('markdown_path') or '')
    if not path.is_absolute():
        path = Path(capture_root) / path
    if not path.is_file():
        raise ValueError('capture body missing: ' + key)
    if sha(path) != cap['content_sha256']:
        raise ValueError('capture hash mismatch: ' + key)
    return path.read_text()


def target_keys(pid, matrix, claim):
    """Map a claim to exact key steps inside the pricing matrix."""
    kind, field = claim.get('kind'), claim.get('field')
    if kind == 'price_component':
        if field not in PRICE_FIELDS:
            raise ValueError('unsupported price component: %s' % field)
        rows = matrix['providers'][pid].get('medications', [])
        index = [i for i, row in enumerate(rows) if row.get('row_id') == claim.get('row_id')]
        if len(index) != 1:
            raise ValueError('price row not uniquely present: %s' % claim.get('row_id'))
        return ('providers', pid, 'medications', index[0], 'facts', field)
    if kind == 'cancellation_terms':
        if field not in CANCELLATION_FIELDS:
            raise ValueError('unsupported cancellation term: %s' % field)
        return ('providers', pid, 'cancellation_terms', 'facts', field)
    raise ValueError('unsupported claim kind: %s' % kind)


def json_pointer(keys):
    return ''.join('/' + pointer(str(key)) for key in keys)


def keys_get(document, keys):
    value = document
    for key in keys:
        value = value[int(key)] if isinstance(value, list) else value.get(key) if isinstance(value, dict) else None
        if value is None:
            return None
    return value


def latest_stored_capture(fact):
    times = []
    for item in (fact or {}).get('evidence', []) or []:
        try:
            times.append(parse_utc(item.get('captured_at_utc')))
        except (ValueError, AttributeError):
            continue
    return max(times) if times else None


def check_claim(claim, captures, capture_root, hosts, as_of, max_age_days, texts):
    errors = []
    for field in PROVENANCE:
        if not claim.get(field):
            errors.append('missing ' + field)
    if 'value' not in claim:
        errors.append('missing value')
    elif claim['value'] is None or claim['value'] == 'UNKNOWN':
        errors.append('unknown value is not a delta')
    key = claim.get('capture_key')
    cap = captures.get(key)
    if cap is None:
        return errors + ['capture not in manifest: %s' % key]
    if key not in texts:
        try:
            texts[key] = load_capture(key, cap, capture_root)
        except ValueError as exc:
            texts[key] = exc
    if isinstance(texts[key], ValueError):
        return errors + [str(texts[key])]
    for field in PROVENANCE:
        if claim.get(field) and claim[field] != cap[field]:
            errors.append(field + ' does not match capture manifest')
    if not safe_url(cap['source_url'], hosts):
        errors.append('source is not an allowed first-party URL')
    quote = claim.get('quote')
    if not quote or quote not in texts[key]:
        errors.append('exact quote absent from cited capture')
    elif 'value' in claim and claim['value'] is not None and str(claim['value']) not in quote:
        errors.append('claimed value absent from quote')
    try:
        captured = parse_utc(cap['captured_at_utc'])
    except ValueError as exc:
        return errors + [str(exc)]
    if captured > as_of:
        errors.append('STALE: capture time after branch as_of')
    elif as_of - captured > timedelta(days=max_age_days):
        errors.append('STALE: capture older than %d days at as_of' % max_age_days)
    return errors


def monthly_branch(pid, month, as_of, claims_doc, matrix_path, capture_root,
                   held_providers=(), max_age_days=MAX_EVIDENCE_AGE_DAYS):
    """Return the monthly F04 branch: exact diff, disposition, F06 intent."""
    matrix_path = Path(matrix_path)
    source_sha = sha(matrix_path)
    matrix = read(matrix_path)
    month_start, month_end = month_bounds(month)
    as_of_dt = parse_utc(as_of)
    if not month_start <= as_of_dt < month_end:
        raise ValueError('as_of outside branch month')
    refusals, branch_errors = [], []
    if claims_doc.get('schema') != CLAIMS_SCHEMA:
        branch_errors.append('unknown claims schema')
    if claims_doc.get('program_id') != pid:
        branch_errors.append('claims program mismatch')
    if any(claims_doc.get(k) not in (None, False) for k in PUBLICATION_KEYS):
        branch_errors.append('PUBLICATION_CLAIM_WITHOUT_F06: observations never claim a published Price Index')
    provider = matrix.get('providers', {}).get(pid)
    if provider is None:
        branch_errors.append('provider absent from pricing matrix')
    elif is_held(pid, provider, set(held_providers)):
        branch_errors.append('HELD: provider is held or permanently excluded')
    captures = claims_doc.get('captures') or {}
    claims = claims_doc.get('claims') or []
    if not claims:
        branch_errors.append('no claims')
    hosts = set(claims_doc.get('first_party_hosts') or [])
    if not hosts:
        branch_errors.append('first-party host allowlist missing')
    targets, texts = {}, {}
    if not branch_errors:
        for index, claim in enumerate(claims):
            errors = check_claim(claim, captures, capture_root, hosts, as_of_dt, max_age_days, texts)
            keys = None
            try:
                keys = target_keys(pid, matrix, claim)
            except (ValueError, KeyError) as exc:
                errors.append(str(exc))
            path = json_pointer(keys) if keys else None
            if keys and not errors:
                stored_latest = latest_stored_capture(keys_get(matrix, keys))
                if stored_latest and parse_utc(claim['captured_at_utc']) <= stored_latest:
                    errors.append('STALE: not newer than stored evidence')
            if errors:
                refusals.append({'claim_index': index, 'target': path, 'reasons': errors})
            elif keys:
                targets.setdefault(keys, []).append((index, claim))
    for keys, items in sorted(targets.items(), key=lambda kv: json_pointer(kv[0])):
        path = json_pointer(keys)
        values = {json.dumps(claim['value'], sort_keys=True) for _, claim in items}
        if len(values) > 1:
            for index, _ in items:
                refusals.append({'claim_index': index, 'target': path,
                                 'reasons': ['CONFLICT: %d distinct values for target' % len(values)]})
    next_matrix = copy.deepcopy(matrix)
    if not branch_errors and not refusals:
        for keys, items in targets.items():
            node = next_matrix
            for key in keys[:-1]:
                node = node[key] if isinstance(node, list) else node.setdefault(key, {})
            node[keys[-1]] = {
                'value': copy.deepcopy(items[0][1]['value']),
                'evidence': [{'source_url': c['source_url'], 'content_sha256': c['content_sha256'],
                              'captured_at_utc': c['captured_at_utc'], 'capture_key': c['capture_key'],
                              'quote': c['quote']} for _, c in sorted(items, key=lambda x: x[0])],
            }
    operations = diff(matrix, next_matrix) if not branch_errors and not refusals else []
    if branch_errors or refusals:
        disposition = 'REFUSED'
    else:
        disposition = 'ACCEPTED' if operations else 'NO_CHANGE'
    registry_diff = {'schema': PATCH_SCHEMA, 'applied_to_candidate': False, 'targets': [{
        'path': str(matrix_path), 'source_sha256': source_sha,
        'result_sha256': stable(next_matrix) if operations else stable(matrix),
        'operations': operations}]}
    acceptance = {'disposition': disposition, 'program_id': pid, 'month': month, 'as_of': as_of,
                  'max_evidence_age_days': max_age_days, 'branch_errors': branch_errors,
                  'refused_claims': sorted(refusals, key=lambda r: r['claim_index']),
                  'accepted_claims': sorted(i for items in targets.values() for i, _ in items) if disposition != 'REFUSED' else [],
                  'adoption_requires': ['source_sha256 unchanged at adoption', 'recheck of capture hashes and freshness']}
    intent = {'schema': INTENT_SCHEMA, 'stage': 'F06',
              'intent': 'REQUEST_F06_ACCEPTANCE' if disposition == 'ACCEPTED' else 'NONE',
              'price_index_published': False, 'publication_claimed': False,
              'registry_diff_sha256': stable(registry_diff),
              'basis': 'Dated first-party observations only; publication requires F06 current-release acceptance and explicit public-release authority.',
              'requires': ['F06 current-release acceptance binding', 'explicit public-release authority']}
    return {'schema': BRANCH_SCHEMA, 'program_id': pid, 'month': month, 'as_of': as_of,
            'registry_diff': registry_diff, 'acceptance': acceptance, 'f06_publication_intent': intent,
            'provenance': 'Reported information; proposed only'}


def adopt(branch, claims_doc, matrix_path, capture_root, adoption_as_of, held_providers=()):
    """Re-verify a branch against current bytes and return the patched matrix.

    Adoption re-runs the full monthly gate at ``adoption_as_of`` so a claim
    that has since gone stale, a changed capture or a drifted matrix refuses.
    """
    if branch.get('schema') != BRANCH_SCHEMA or branch['acceptance']['disposition'] != 'ACCEPTED':
        raise ValueError('only an ACCEPTED branch can be adopted')
    if branch['f06_publication_intent'].get('price_index_published') is not False:
        raise ValueError('branch claims publication')
    target = branch['registry_diff']['targets'][0]
    if sha(matrix_path) != target['source_sha256']:
        raise ValueError('stale delta: pricing matrix changed since branch')
    replay = monthly_branch(branch['program_id'], branch['month'], adoption_as_of, claims_doc,
                            matrix_path, capture_root, held_providers,
                            branch['acceptance']['max_evidence_age_days'])
    if replay['acceptance']['disposition'] != 'ACCEPTED':
        raise ValueError('adoption refused: ' + json.dumps(replay['acceptance']['branch_errors']
                                                          + replay['acceptance']['refused_claims']))
    if replay['registry_diff']['targets'][0]['operations'] != target['operations']:
        raise ValueError('adoption refused: delta differs from reviewed branch')
    adopted = apply_patch(read(matrix_path), target['operations'])
    if stable(adopted) != target['result_sha256']:
        raise ValueError('adoption refused: result hash mismatch')
    return adopted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program', required=True)
    parser.add_argument('--month', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--claims', type=Path, required=True)
    parser.add_argument('--matrix', type=Path, required=True)
    parser.add_argument('--capture-root', type=Path, required=True)
    parser.add_argument('--held', action='append', default=[])
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve() == args.matrix.resolve():
        parser.error('output must not overwrite the matrix')
    branch = monthly_branch(args.program, args.month, args.as_of, read(args.claims), args.matrix,
                            args.capture_root, args.held)
    put(args.out, branch)
    print(json.dumps({'program_id': args.program, 'disposition': branch['acceptance']['disposition'],
                      'operations': len(branch['registry_diff']['targets'][0]['operations']),
                      'branch_sha256': digest(json.dumps(branch, sort_keys=True))}))
    return 0 if branch['acceptance']['disposition'] != 'REFUSED' else 1


if __name__ == '__main__':
    sys.exit(main())
