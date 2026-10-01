#!/usr/bin/env python3
"""Build an existing fanout's detached site source; never deploy or visit links.

The caller supplies a disposable candidate source tree, not a canonical/sealed
release. Registry mutation belongs to affiliate_terms_fanout.consume. This
consumer uses the site's maintained build-staging.mjs and rejects stale rendered
links/pages. Its receipt is NOT package/publication approval.
"""
from pathlib import Path
import argparse
import html
import json
import re
import subprocess
import affiliate_terms_fanout as fanout


def assert_link_removal(source: Path, program: dict, retired_tracking_link: str = '') -> dict:
    public = source / 'public'
    if not public.is_dir():
        raise ValueError('withdrawal_candidate_public_missing')
    review = program['review_path']
    if (public / review.lstrip('/') / 'index.html').exists():
        raise ValueError('withdrawal_candidate_review_still_rendered')
    routes = {program['go']['route'], review}
    if program.get('review_slug'):
        routes.add('/go/' + program['review_slug'])
    forbidden = []
    checked = 0
    for path in sorted(public.rglob('*')):
        if not path.is_file() or (path.suffix not in {'.html', '.xml', '.json', '.js', '.txt'} and path.name != '_redirects'):
            continue
        checked += 1
        text = path.read_text(errors='replace')
        if path.suffix == '.json':
            def strings(value):
                if isinstance(value, str):
                    return [value]
                if isinstance(value, dict):
                    return [s for v in value.values() for s in strings(v)]
                if isinstance(value, list):
                    return [s for v in value for s in strings(v)]
                return []
            text += '\n' + '\n'.join(strings(json.loads(text)))
        text = html.unescape(text).replace(chr(92) + chr(34), chr(34))
        # Ignore identity retained as history in registry/held worker metadata,
        # but refuse any executable HTML link or sitemap/redirect to the page.
        hrefs = re.findall(r'(?:href|action)\s*=\s*[\"\x27]([^\"\x27]+)', text, re.I)
        if any(any(url.split('?', 1)[0].endswith(route) for route in routes) for url in hrefs):
            forbidden.append(str(path.relative_to(public)))
        if path.suffix == '.xml' and review in text:
            forbidden.append(str(path.relative_to(public)))
        if retired_tracking_link and retired_tracking_link in text:
            forbidden.append(str(path.relative_to(public)))
        if path.name == '_redirects' and any(route in text for route in routes):
            forbidden.append(str(path.relative_to(public)))
    if forbidden:
        raise ValueError('withdrawal_candidate_stale_links:' + ','.join(sorted(set(forbidden))))
    quiz = public / 'quiz/provider-fit/index.html'
    if not quiz.is_file():
        raise ValueError('withdrawal_candidate_quiz_payload_missing')
    match = re.search(r'<script[^>]*id="candidate-data"[^>]*>(.*?)</script>', quiz.read_text(), re.S)
    if not match:
        raise ValueError('withdrawal_candidate_quiz_payload_missing')
    data = json.loads(match.group(1))
    ids = {program['program_id'], program.get('review_slug')}
    if any(row.get('program_id') in ids for row in data):
        raise ValueError('withdrawal_candidate_quiz_still_eligible')
    return {'checked_public_files': checked, 'review_absent': True, 'quiz_ineligible': True,
            'affiliate_hrefs_absent': True}


def build(request_path: Path, source: Path, *, runner=subprocess.run) -> dict:
    request = fanout.read(request_path)
    source = source.resolve(strict=True)
    if (request.get('link_removal_candidate_required') is not True
            or request.get('registry_path') != str(source / 'data/program-registry.json')
            or not any('candidate' in part for part in source.parts)
            or any(part in {'releases', 'sealed'} for part in source.parts)):
        raise ValueError('withdrawal_detached_candidate_source_required')
    registry_path = source / 'data/program-registry.json'
    if fanout.digest(registry_path.read_bytes()) != request['registry_after_sha256']:
        raise ValueError('withdrawal_candidate_registry_drift')
    programs = [p for p in fanout.read(registry_path)['programs'] if p['program_id'] == request['registry_program_id']]
    if len(programs) != 1 or programs[0].get('affiliate_program_state') != 'withdrawn':
        raise ValueError('withdrawal_candidate_program_not_withdrawn')
    builder = source / 'scripts/build-staging.mjs'
    if not builder.is_file() or builder.is_symlink():
        raise ValueError('withdrawal_native_builder_missing')
    argv = ['node', str(builder)]
    outcome = runner(argv, cwd=source, capture_output=True, timeout=900, check=False)
    receipt = {'schema': 'glp.affiliate_withdrawal_candidate.v1', 'candidate_only': True,
               'site_released': False, 'release_authority': False, 'site_dispatched': False,
               'command': argv, 'returncode': outcome.returncode,
               'request': fanout.binding(request_path), 'registry': fanout.binding(registry_path),
               'builder': fanout.binding(builder), 'stdout_sha256': fanout.digest(outcome.stdout),
               'stderr_sha256': fanout.digest(outcome.stderr)}
    if outcome.returncode != 0:
        receipt['status'] = 'BLOCKED_NATIVE_BUILD'
    else:
        if fanout.digest(registry_path.read_bytes()) != request['registry_after_sha256']:
            raise ValueError('withdrawal_native_build_registry_changed')
        receipt['readback'] = assert_link_removal(source, programs[0], request.get('retired_tracking_link') or '')
        receipt['status'] = 'PASS_LINK_REMOVAL_CANDIDATE_NEEDS_RELEASE_REVIEW'
    fanout.atomic(request_path.parent / 'link-removal-build.json', fanout.encoded(receipt))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-request', type=Path, required=True)
    parser.add_argument('--candidate-source', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.candidate_request, args.candidate_source)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['returncode'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
