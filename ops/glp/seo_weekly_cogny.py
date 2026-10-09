"""Weekly Cogny evidence adapter. No scheduler, release authority or implicit I/O.

Use the existing Cogny client with retries=1. All responses and exact query
arguments are bound in the caller's exclusive Store; no credentials are read here.
URL-prefix evidence remains supplemental when the domain property is unavailable.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import os
import uuid
import hashlib
import json
import math
from pathlib import Path

from ops.glp.cogny_client import TOOL
from ops.glp.seo_data_rails import GoogleRail

DOMAIN = 'sc-domain:glpclinicscout.com'
PREFIX = 'https://glpclinicscout.com/'
HOST = 'glpclinicscout.com'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def unwrap(value):
    for _ in range(4):
        if isinstance(value, dict) and any(value.get(k) for k in ('error', 'cogny_error', 'isError')):
            raise ValueError('provider_error_envelope')
        if isinstance(value, str):
            value = json.loads(value)
        elif isinstance(value, dict) and 'result' in value and not all(k in value for k in ('id', 'title', 'body')):
            value = value['result']
        elif isinstance(value, dict) and 'd' in value:
            value = value['d']
        else:
            break
    if isinstance(value, dict) and any(value.get(k) for k in ('error', 'cogny_error', 'isError')):
        raise ValueError('provider_error_envelope')
    return value


def collect(client, store, report_date, *, row_limit=25000, max_pages=40):
    """Collect exact 28/90/prior28 query×page, GA4 routes and Bing provider stats.

    complete means pagination finished over available API rows, NOT whole-site
    coverage. GSC's schema cannot set dataState: record default, not "final".
    Missing properties/metrics and malformed responses never become zero.
    """
    if not 1 <= row_limit <= 25000 or not 1 <= max_pages <= 40:
        raise ValueError('collection_bounds_invalid')
    queries, snapshots = [], {}

    def call(tool, arguments):
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            response = client.call(tool, arguments)
            value = unwrap(response)
            state = 'RETURNED'
        except Exception as exc:
            response = {'error_type': type(exc).__name__}
            value, state = None, 'UNKNOWN_TRANSPORT_OR_PROVIDER'
        receipt = store.save(f'cogny/queries/{len(queries):03d}.json', {
            'tool': tool, 'arguments': arguments, 'request_started_at_utc': started,
            'captured_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'state': state, 'response': response})
        queries.append({'query_id': digest([tool, arguments]), 'tool': tool,
                        'arguments': arguments, 'state': state, 'receipt': receipt})
        return value

    def save(name, context, result, first):
        snapshots[name] = store.save(f'cogny/snapshots/{name}.json', {
            'captured_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'context': {'rail': 'Cogny', **context}, 'result': result,
            'queries': queries[first:]})

    properties = call(TOOL['gsc_list_properties'], {})
    if isinstance(properties, dict):
        properties = properties.get('siteEntry', properties.get('properties'))
    urls = {p.get('siteUrl') for p in properties if isinstance(p, dict)} if isinstance(properties, list) else set()
    prop = DOMAIN if DOMAIN in urls else PREFIX if PREFIX in urls else None
    end = report_date - dt.timedelta(days=3)
    for name, days, stop in [('gsc-28', 28, end), ('gsc-90', 90, end),
                             ('gsc-prior-28', 28, end-dt.timedelta(days=28))]:
        start = stop - dt.timedelta(days=days-1)
        context = {'property': prop, 'start_date': str(start), 'end_date': str(stop),
                   'window_days': days, 'dimensions': ['query', 'page'], 'search_type': 'web',
                   'data_state': 'provider_default_not_explicitly_set',
                   'privacy_omissions_remain': True, 'complete_means_available_API_rows_only': True}
        rows, seen, first, complete = [], set(), len(queries), False
        if prop:
            for page in range(max_pages):
                doc = call(TOOL['gsc_search_analytics'], {
                    'site_url': prop, 'start_date': str(start), 'end_date': str(stop),
                    'dimensions': ['query', 'page'], 'search_type': 'web',
                    'row_limit': row_limit, 'start_row': page*row_limit,
                    'aggregation_type': 'auto'})
                if isinstance(doc, dict) and 'rows' not in doc and doc.get('responseAggregationType'):
                    doc = {**doc, 'rows': []}
                batch = doc.get('rows') if isinstance(doc, dict) else None
                if not isinstance(batch, list) or len(batch) > row_limit:
                    break
                if any(not isinstance(r, dict) or not isinstance(r.get('keys'), list) or len(r['keys']) != 2
                       or not all(isinstance(k, str) for k in r['keys']) for r in batch):
                    break
                keys = [tuple(r['keys']) for r in batch]
                if len(set(keys)) != len(keys) or any(k in seen for k in keys):
                    break
                seen.update(keys)
                rows.extend(batch)
                if len(batch) < row_limit:
                    complete = True
                    break
        save(name, context, {'ok': complete, 'complete': complete, 'rows': rows,
                            'state': 'OBSERVED' if complete else 'UNKNOWN_PARTIAL_OR_PROPERTY_UNAVAILABLE'}, first)

    first, rows, complete, seen, total = len(queries), [], False, set(), None
    ga_start = end - dt.timedelta(days=27)
    metrics = ['screenPageViews', 'sessions']
    for page in range(max_pages):
        doc = call(TOOL['ga4_run_report'], {
            'property_id': '541440529', 'date_ranges': [{'startDate': str(ga_start), 'endDate': str(end)}],
            'dimensions': ['streamId', 'hostName', 'pagePath'], 'metrics': metrics,
            'dimension_filter': GoogleRail.ga4_neutral_filter(),
            'limit': row_limit, 'offset': page*row_limit})
        batch = doc.get('rows') if isinstance(doc, dict) else None
        count = doc.get('rowCount') if isinstance(doc, dict) else None
        if not isinstance(batch, list) or type(count) is not int or count < 0 or len(batch) > row_limit:
            break
        if total is not None and total != count:
            break
        total = count
        valid = []
        try:
            for row in batch:
                key = (row['hostName'], row['pagePath'])
                if (row['streamId'] != GoogleRail.GA4_CONSUMER_STREAM_ID
                        or key[0] != HOST or key[1] not in GoogleRail.GA4_NEUTRAL_PATHS or key in seen):
                    raise ValueError('ga4_scope_or_duplicate')
                seen.add(key)
                values = []
                for metric in metrics:
                    value = row.get(metric)
                    value = float(value) if value is not None and not isinstance(value, bool) else None
                    if value is None or not math.isfinite(value) or value < 0:
                        raise ValueError('ga4_metric_invalid')
                    values.append(value)
                valid.append({'dimensions': list(key), 'metrics': values})
        except (KeyError, TypeError, ValueError):
            break
        rows.extend(valid)
        if len(rows) == total:
            complete = True
            break
        if len(batch) < row_limit or len(rows) > total:
            break
    save('ga4', {'property': '541440529', 'hostname': HOST, 'start_date': str(ga_start),
                 'end_date': str(end), 'dimensions': ['hostName', 'pagePath'],
                 'consumer_stream_id': GoogleRail.GA4_CONSUMER_STREAM_ID,
                 'neutral_route_paths': list(GoogleRail.GA4_NEUTRAL_PATHS),
                 'scope': 'exact_consumer_stream_host_and_approved_neutral_routes',
                 'article_measurement': 'UNMEASURED_BY_DESIGN',
                 'affiliate_cash_attribution': 'UNKNOWN_NOT_JOINED'},
         {'ok': complete, 'complete': complete, 'rows': rows if complete else [], 'metric_names': metrics,
          'row_count_provider': total}, first)
    for name, tool in [('bing-query', 'bing_query_stats'), ('bing-page', 'bing_page_stats')]:
        first = len(queries)
        result = call(TOOL[tool], {'site_url': PREFIX.rstrip('/')})
        valid = isinstance(result, list) and all(isinstance(r, dict) for r in result)
        save(name, {'site_url': PREFIX.rstrip('/'),
                    'provider_period': 'provider_defined_not_28_or_90_days',
                    'collector_freshness_is_not_provider_reporting_freshness': True},
             {'ok': valid, 'complete': valid, 'coverage': 'provider_top_rows_not_full_site',
              'rows': result if valid else None}, first)
    result = {'schema': 'glp-weekly-cogny-evidence-v1', 'report_date': str(report_date),
              'snapshots': snapshots, 'queries': queries, 'domain_property_available': prop == DOMAIN,
              'publication_authorized': False}
    result['binding'] = store.save('cogny/EVIDENCE.json', result)
    return result


def read_binding(binding, *, text=False):
    path = Path(binding['path'])
    if not path.is_absolute():
        raise ValueError('binding_path_not_absolute')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != binding['sha256']:
        raise ValueError('binding_hash_changed')
    value = raw.decode('utf-8')
    if text and 'text' in binding and value != binding['text']:
        raise ValueError('binding_loaded_text_changed')
    return value if text else json.loads(value)


# Exact excerpts from the six loaded owners, validated at every strategy run.
# These are applied below, not a ceremonial list of skill names.
RULES = {
    'comparison-site-seo': 'Preserve legal, login/identity, spend, privacy, send, release, queue and ambiguous-effect gates.',
    'comparison-site-programmatic-seo': 'Start with a distinct consumer decision, not a keyword permutation.',
    'gsc-page-two-optimization': 'Treat average position as a diagnostic, not a stable page-two rank.',
    'intent-internal-linking': 'Model links as helpful next steps within a consumer task.',
    'provider-review-seo': 'Do not change dates without substantial content changes.',
    'search-console-bing-operations': 'GSC typical 2–3d reporting and Bing GetPageStats/GetQueryStats weekly updates do not extend collector last-success freshness.',
}


def enrich_strategy(strategy, modules_report, evidence, business_context, seo_skills):
    """Attach applied rules and exact evidence without rewriting source intakes.

    Parent loads business context and six owners at job start with
    seo_business_context.load_business_context/load_seo_skills. This validates
    their bytes again and consumes their text. No helper import or network I/O.
    """
    context_text = read_binding(business_context, text=True)
    if not context_text.strip():
        raise ValueError('business_context_empty')
    citations = {}
    for name, quote in RULES.items():
        loaded = seo_skills[name]
        text = read_binding(loaded, text=True)
        if quote not in text:
            raise ValueError('applied_skill_rule_changed:' + name)
        citations[name] = {'skill': name, 'path': loaded['path'], 'sha256': loaded['sha256'], 'quote': quote}
    if 'binding' in evidence:
        saved = read_binding(evidence['binding'])
        if saved != {k: v for k, v in evidence.items() if k != 'binding'}:
            raise ValueError('evidence_binding_projection_changed')
    queries = evidence['queries']
    for query in queries:
        saved = read_binding(query['receipt'])
        if any(saved.get(k) != query.get(k) for k in ('tool', 'arguments', 'state')):
            raise ValueError('query_binding_projection_changed')
    docs = {name: read_binding(bound) for name, bound in evidence['snapshots'].items()}
    for doc in docs.values():
        captured = dt.datetime.fromisoformat(doc['captured_at_utc'].replace('Z', '+00:00'))
        if captured.tzinfo is None or not -360 <= (dt.datetime.now(dt.timezone.utc)-captured).total_seconds() <= 10800:
            raise ValueError('evidence_binding_stale_or_future')
    diagnostics = {}
    for name in ('gsc-28', 'gsc-90'):
        snap = docs[name]
        result = snap['result']
        complete = result.get('ok') is True and result.get('complete') is True
        rows = result.get('rows', []) if complete else []
        def num(row, field):
            value = row.get(field)
            return value if type(value) in (int, float) and math.isfinite(value) else None
        page2 = [r for r in rows if num(r, 'position') is not None and 11 <= r['position'] <= 20]
        # 28d is the operational monthly proxy; 90d is context, never added to 28d.
        diagnostic = {'context': snap['context'], 'status': 'OBSERVED' if complete else 'UNKNOWN',
                      'rows': len(rows) if complete else None,
                      'page2_at_least_100_impressions': sum(num(r, 'impressions') is not None and r['impressions'] >= 100 for r in page2) if complete else None,
                      'page2_below_100_impressions': sum(num(r, 'impressions') is not None and r['impressions'] < 100 for r in page2) if complete else None,
                      'page2_unknown_impressions': sum(num(r, 'impressions') is None for r in page2) if complete else None,
                      'money_push_position_4_15': sum(num(r, 'position') is not None and 4 <= r['position'] <= 15 for r in rows) if complete else None,
                      'money_push_scope': 'candidate query/page diagnostics, not confirmed money-page or intended-query matches',
                      'threshold_scope': '100 per returned window; use only 28d as monthly proxy, 90d context is not monthly qualification'}
        diagnostics[name] = diagnostic
    def refs(names):
        return [{'snapshot': name, **evidence['snapshots'][name],
                 'context': docs[name]['context'], 'observed_ok': docs[name]['result'].get('ok') is True,
                 'queries': docs[name]['queries']} for name in names]
    def rules(*names):
        return [citations[name] for name in names]
    recommendations = [
        {'action': 'Prioritize supported query/page improvements; 11–20 is diagnostic. Keep sub-100-impression rows visible as low-volume context, not proven goldmines. Audit intended versus accidental ranking before a positions 4–15 optimization push.',
         'rules_applied': rules('gsc-page-two-optimization', 'comparison-site-programmatic-seo'),
         'cogny_evidence': refs(['gsc-28', 'gsc-90']), 'diagnostics': diagnostics},
        {'action': 'Review accurate provider comparison copy and contextual next-decision links. Freshness means substantive source-backed changes, never a date-only refresh or keyword-permutation doorway.',
         'rules_applied': rules('provider-review-seo', 'intent-internal-linking', 'comparison-site-programmatic-seo'),
         'cogny_evidence': refs(['gsc-90', 'ga4']),
         'qualification': 'GA4 engagement is not cash and does not gate outward content; source-copy and provider holds remain binding.'},
        {'action': 'Track Bing observations with provider dates separately from collector timestamps; preserve missing observations as UNKNOWN and use the existing publication-coupled indexing owner.',
         'rules_applied': rules('search-console-bing-operations', 'comparison-site-seo'),
         'cogny_evidence': refs(['bing-query', 'bing-page']),
         'qualification': 'Provider top rows are not complete site coverage; collector execution is not indexation or publication.'},
    ]
    actions = []
    for original in strategy.get('next_patch_actions', []):
        action = {**original, 'rules_applied': rules('comparison-site-seo', 'comparison-site-programmatic-seo',
                    'gsc-page-two-optimization', 'provider-review-seo'),
                  'cogny_evidence': refs(['gsc-28', 'gsc-90']),
                  'cogny_evidence_role': 'supplemental_not_source_copy_authority',
                  'source_evidence_bindings': modules_report.get('bindings', {}),
                  'publication_authorized': False}
        actions.append(action)
        recommendations.append(action)
    lines = [strategy.get('markdown', ''), '', '## Applied owner rules and Cogny evidence',
             f"Business context: {business_context['path']} SHA256 {business_context['sha256']}",
             'GA4 engagement is not cash. Missing or prefix-only evidence never substitutes for domain-property source authority.']
    for name, cite in citations.items():
        lines.append(f"- {name}: {cite['quote']} [{cite['path']} SHA256 {cite['sha256']}]")
    for query in queries:
        lines.append(f"- Cogny `{query['tool']}` {json.dumps(query['arguments'], sort_keys=True)} → {query['state']}; {query['receipt']['path']} SHA256 {query['receipt']['sha256']}")
    for rec in recommendations:
        lines.append('- ' + rec['action'] + ' Rules: ' + ', '.join(r['skill'] for r in rec['rules_applied'])
                     + '; Cogny snapshots: ' + ', '.join(r['path'] for r in rec['cogny_evidence']))
    lines.append('Search diagnostics (non-additive windows): ' + json.dumps(diagnostics, sort_keys=True))
    return {**strategy, 'business_context': {k: business_context[k] for k in ('path', 'sha256')},
            'skills_applied': citations, 'cogny_queries': queries, 'search_diagnostics': diagnostics,
            'recommendations': recommendations, 'next_patch_actions': actions,
            'publication_authorized': False, 'own_site_analytics_gates_content': False,
            'markdown': '\n'.join(lines)}


def _atomic(path, doc):
    raw = (json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}


def ensure_weekly_finding(client, journal_root, strategy):
    """One stable week ticket with write-ahead custody and exact remote readback.

    Call under the weekly owner lock. A second local lock protects this journal.
    A lost create response is reconciled by title; absent/incomplete lookup
    cannot authorize retry. A changed same-week strategy requires owner review,
    never silent update/replacement. Journal retention is required for dedupe.
    """
    if getattr(client, '_retries', None) != 1:
        raise ValueError('finding_requires_single_attempt_client')
    week = dt.date.fromisoformat(strategy['week_start'])
    if week.weekday() != 0 or str(week) != strategy['week_start']:
        raise ValueError('finding_week_must_be_exact_monday')
    payload = {
        'title': f'GLP weekly SEO strategy — {week}',
        'body': 'Evidence-bound weekly recommendations; not publication approval.\n'
                + json.dumps(strategy, sort_keys=True, ensure_ascii=False, allow_nan=False),
        'action_type': 'weekly_seo_strategy', 'priority': 'medium',
        'expected_outcome': 'Review supported source improvements; ranking and affiliate cash uplift UNKNOWN.'}
    root = Path(journal_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / (str(week) + '.json')
    with (root / (str(week) + '.lock')).open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else None
        if state is not None and (state.get('payload_sha256') != digest(payload) or state.get('payload') != payload):
            raise ValueError('finding_week_payload_changed_reconcile')
        finding_id = state.get('finding_id') if state else None
        if not finding_id:
            listing = unwrap(client.call('list_findings', {
                'status': ['open', 'in_progress', 'done', 'dismissed'], 'limit': 10000}))
            rows = listing.get('findings') if isinstance(listing, dict) else listing
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError('finding_inventory_unknown_no_create')
            matches = [r for r in rows if r.get('title') == payload['title']]
            if len(matches) > 1:
                raise ValueError('finding_duplicate_week_reconcile')
            if matches:
                finding_id = matches[0].get('id') or matches[0].get('finding_id')
                if not finding_id:
                    raise ValueError('finding_identity_missing')
            else:
                if state is not None:
                    raise ValueError('finding_ambiguous_attempt_not_visible_no_retry')
                if (len(rows) >= 10000 or (isinstance(listing, dict) and
                    (listing.get('has_more') or listing.get('hasMore') or
                     listing.get('total', len(rows)) != len(rows)))):
                    raise ValueError('finding_inventory_incomplete_no_create')
                state = {'state': 'ATTEMPTED_NO_BLIND_RETRY', 'payload': payload,
                         'payload_sha256': digest(payload), 'week_start': str(week)}
                _atomic(path, state)
                try:
                    created = unwrap(client.call('create_finding', payload))
                    created = created.get('finding', created) if isinstance(created, dict) else {}
                    finding_id = created.get('id') or created.get('finding_id')
                    if not isinstance(finding_id, str) or not finding_id:
                        raise ValueError('missing_created_id')
                except Exception:
                    raise ValueError('finding_create_ambiguous_reconcile_before_retry') from None
        state = state or {'payload': payload, 'payload_sha256': digest(payload), 'week_start': str(week)}
        state.update(state='READBACK_REQUIRED', finding_id=finding_id)
        _atomic(path, state)
        remote = unwrap(client.call('get_finding', {'finding_id': finding_id}))
        remote = remote.get('finding', remote) if isinstance(remote, dict) else {}
        if ((remote.get('id') or remote.get('finding_id')) != finding_id or
                any(remote.get(k) != v for k, v in payload.items())):
            raise ValueError('finding_exact_readback_mismatch')
        readback = _atomic(root / (str(week) + '-readback.json'), {
            'tool': 'get_finding', 'arguments': {'finding_id': finding_id},
            'captured_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'response': remote})
        state.update(state='VERIFIED_EXACT_READBACK', readback=readback)
        journal = _atomic(path, state)
        return {'status': state['state'], 'finding_id': finding_id,
                'week_start': str(week), 'journal': journal, 'readback': readback}
