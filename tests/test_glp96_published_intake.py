"""Published observations reconcile into existing intake, never approval or send."""
import copy
import importlib


def test_published_nonaffiliate_is_matched_to_existing_claimed_queue():
    intake = importlib.import_module('scripts.glp_published_provider_intake')
    published = [{'provider_id': 'domain:care.example', 'name': 'Example Care',
                  'pages': ['https://glpclinicscout.com/guides/example/'],
                  'scope': 'primary_service_subject', 'slugs': ['example-care']}]
    queue = [{'queue_id': 'original', 'program_name': 'Example Care',
              'network': 'direct', 'program_id': 'domain:care.example', 'status': 'claimed'}]
    original = copy.deepcopy(queue)
    result = intake.reconcile(published, sheet=[], application_queue=queue,
                              sheet_queue=[], ledger=[], relationships=[], holds=[])
    assert result['counts']['published'] == 1
    assert result['counts']['matched'] == 1
    assert result['providers'][0]['intake_matches'][0]['status'] == 'claimed'
    assert result['providers'][0]['next_action'] == 'existing_intake'
    assert queue == original
    assert result['effects']['applications_submitted'] == 0


def test_negative_hold_survives_existing_queue_and_duplicate_publication():
    intake = importlib.import_module('scripts.glp_published_provider_intake')
    item = {'provider_id': 'domain:fellahealth.com', 'name': 'Fella Health',
            'pages': ['https://glpclinicscout.com/reviews/fella/'], 'slugs': ['fella']}
    result = intake.reconcile([item, item], sheet=[], application_queue=[],
        sheet_queue=[], ledger=[], relationships=[], holds=[])
    assert result['counts'] == {'published': 1, 'held': 1}
    assert result['providers'][0]['next_action'] == 'preserve_hold'


def test_missing_unknown_and_outside_population_are_not_zero():
    intake = importlib.import_module('scripts.glp_published_provider_intake')
    rows = [{'provider_id': 'domain:new.example', 'name': 'New Care', 'pages': ['site']},
            {'provider_id': 'domain:old.example', 'name': 'Old Care', 'pages': ['site'],
             'scope': 'historical_identity_unresolved'},
            {'provider_id': 'domain:conflict.example', 'name': 'Conflict', 'pages': ['site'],
             'identity_conflict': {'unproven_alias': True}}]
    result = intake.reconcile(rows, sheet=[], application_queue=[], sheet_queue=[],
                              ledger=[], relationships=[], holds=[])
    assert result['counts'] == {'published': 3, 'missing': 1, 'outside_new_feed': 1, 'unresolved': 1}
    assert result['status'] == 'INCOMPLETE'
    assert all(r['relationship_state'] == 'UNKNOWN' for r in result['providers'])
    assert not any(r['direct_fallback_authorized'] for r in result['providers'])


def test_live_capture_retains_unmapped_published_provider(tmp_path):
    intake = importlib.import_module('scripts.glp_published_provider_intake')
    sitemap = b'<urlset><url><loc>https://glpclinicscout.com/providers/new-care/</loc></url></urlset>'
    responses = {'https://glpclinicscout.com/sitemap.xml': sitemap,
                 'https://glpclinicscout.com/providers/new-care/': b'<main>New care</main>'}
    observed, receipt = intake.capture_published({}, tmp_path, fetch=responses.__getitem__)
    assert receipt['pages_read'] == 1
    assert observed[0]['provider_id'] == 'published:new-care'
    assert observed[0]['identity_conflict'] == 'domain_identity_not_established'


def test_live_discovery_admission_calls_existing_research_feeder_only_on_apply(monkeypatch, tmp_path):
    from scripts import affiliate_auto_apply, glp_application_dedupe
    from scripts import glp_published_provider_intake as feeder
    calls = []
    monkeypatch.setattr(glp_application_dedupe, 'build', lambda **kwargs: object())
    def observe(**kwargs):
        calls.append(kwargs)
        return {'status': 'COMPLETE', 'counts': {'published': 66, 'matched': 54},
                'effects': {'applications_submitted': 0}, 'output': 'fixture-only'}
    monkeypatch.setattr(feeder, 'run', observe)
    plan = affiliate_auto_apply.admit_discovery([], apply=False)
    assert calls == [] and 'published_intake' not in plan
    effect = affiliate_auto_apply.admit_discovery([], apply=True,
                    journal_dir=tmp_path / 'journal', lock_path=tmp_path / 'lock')
    assert calls == [{'apply': True}]
    assert effect['published_intake']['status'] == 'COMPLETE'
    assert effect['effects']['applications_submitted'] == 0


def test_source_bound_research_queue_closes_gap_without_new_application():
    intake = importlib.import_module('scripts.glp_published_provider_intake')
    item = {'provider_id': 'domain:care.example', 'name': 'Care', 'pages': ['site']}
    args = dict(sheet=[], application_queue=[], ledger=[], relationships=[], holds=[])
    first = intake.reconcile([item], sheet_queue=[], **args)
    assert first['status'] == 'INCOMPLETE'
    queued = [{'update_id': 'existing-id', 'program_id': item['provider_id'], 'status': 'queued'}]
    second = intake.reconcile([item], sheet_queue=queued, **args)
    assert second['status'] == 'COMPLETE' and second['counts']['matched'] == 1
    third = intake.reconcile([item], sheet_queue=queued, **args)
    assert second == third
    assert not queued[0].get('operator_authority')


def test_research_fact_has_consumer_lifecycle_and_recovers_only_exact_unclaimed_predecessor():
    from scripts import glp_published_provider_intake as feeder
    stamp = '2026-10-03T00:00:00Z'
    original = {'kind': 'research_only_provider', 'evidence': 'source-bound'}
    fact = feeder.queue_fact(original, observed_at=stamp)
    assert fact['status'] == 'queued' and fact['queued_at_utc'] == stamp and fact['note']
    assert feeder.repairable_queue_row({**original, 'status': None}, original)
    assert not feeder.repairable_queue_row({**original, 'status': 'claimed'}, original)
    assert not feeder.repairable_queue_row({**original, 'evidence': 'drift'}, original)
