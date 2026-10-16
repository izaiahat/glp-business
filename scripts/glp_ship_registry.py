"""Reviewed job/cohort allowlist. No caller-supplied paths, tiers or commands."""
from dataclasses import dataclass
from dataclasses import replace

# Keep a literal declaration: maintenance_inventory reads it without imports.
REPORTING_PATHS = (
    'ops/glp/analytics-brain-control.json',
    'ops/glp/reporting-v2/source-manifest.json',
    'ops/glp/reporting-v2/identity-registry.json',
    'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh',
    'systems/analytics-brain/analytics_brain.py',
    'systems/analytics-brain/collector.py',
    'scripts/glp_affiliate_weekly_reporting.py',
    'scripts/glp_affiliate_weekly_reporting_job.py',
    'scripts/affiliate_network_access.py',
    'scripts/affiliate_reporting_admitad.py',
    'scripts/affiliate_reporting_clickbank.py',
    'scripts/affiliate_reporting_everflow.py',
    'scripts/affiliate_reporting_healthtrader.py',
    'scripts/affiliate_reporting_partnerstack.py',
    'scripts/affiliate_reporting_refersion.py',
    'scripts/glp_network_revenue_import_v2.py',
    'scripts/glp_network_revenue_v2.py',
    'scripts/impact_api_credentials.py',
    'scripts/marketplace_adapters/__init__.py',
    'scripts/marketplace_adapters/_base.py',
    'scripts/marketplace_adapters/affiliati.py',
    'scripts/marketplace_adapters/awin.py',
    'scripts/marketplace_adapters/cj.py',
    'scripts/marketplace_adapters/healthtrader.py',
    'scripts/marketplace_adapters/impact.py',
    'scripts/marketplace_adapters/katalys.py',
    'scripts/marketplace_adapters/partnerize.py',
)


# Finite coherent union of separately accepted PR554 and PR550 owned leaves.
# Source receiving must precede enrollment; this route does not install it.
GLP334_F12_SOURCE = 'b34c5de8fddcbd0bb8b9ecf1ebc04959064b4f4f'
GLP334_SOURCE = '064a9c433c6f473d746aa62aac2a01fbfbcaa388'
GLP334_UNION = 'ops/glp/reporting-v2/glp334-receiving-union.json'
GLP334_PREDECESSOR = 'ebd64f331d3ae825f1fc10de19d260321688accb'
GLP334_PATHS = (
    'scripts/affiliate_cj_checkpoint.py',
    'scripts/affiliate_network_access_j7.py',
    'scripts/glp_affiliate_weekly_reporting_j7.py',
    'scripts/glp_affiliate_weekly_reporting.py',
    'scripts/glp334_live_reconciliation.py',
    'ops/glp/reporting-v2/source-manifest.json',
    'ops/glp/reporting-v2/source-manifest-j5.json',
    'ops/glp/reporting-v2/source-manifest-j6.json',
    'ops/glp/reporting-v2/source-manifest-j7.json',
    'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh',
    'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j5.sh',
    'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j6.sh',
    'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j7.sh',
    'ops/hermes-cron-scripts/glp334_live_reconciliation.sh',
    'systems/analytics-brain/analytics_brain.py',
    'systems/analytics-brain/collector.py',
    'tests/test_glp44_collector_release.py',
)
GLP334_JOBS = (
    ('aadf6677c7f0', 'glp_affiliate_weekly_reporting_j7.sh',
     'b56681704de41039cf39bb35603a3642d170fa19b93c2a016985a227e4591525', 300, '0 9 * * 1'),
    ('2ca359464bfa', 'glp334_live_reconciliation.sh',
     '7ef469a95d9690d25d5c128dc158062d90ac90f5551884cdf4a88000d44d81b5', 180, '15 9 * * *'),
)


@dataclass(frozen=True)
class Job:
    name: str
    kind: str
    tier: str
    paths: tuple
    jobs: tuple
    wrappers: tuple
    window: bool


def receiving_cohort(checkout):
    """Authenticate/dedupe only the two accepted owned lists; never scan main."""
    import json
    import subprocess
    from pathlib import Path
    def git(*args):
        return subprocess.check_output(['git', '-C', str(checkout), *args])
    declaration = json.loads((Path(checkout) / GLP334_UNION).read_bytes())
    f12_head = '2b6f431893db9c86c78d358dab6b907d30cbe4fe'
    analytics_head = '25f9fb7c235e53e4fc31cdee7b0379f28e08f039'
    original = json.loads(git('show', GLP334_F12_SOURCE + ':ops/glp/reporting-v2/glp334-install-cohort.json'))
    f12 = original['selected_paths']
    analytics = git('diff', '--name-only', GLP334_F12_SOURCE, GLP334_SOURCE).decode().splitlines()
    analytics.remove('ops/rebuild-source-manifest.json')
    expected_sources = {
        'PR554': {'merge': GLP334_F12_SOURCE, 'accepted_head': f12_head, 'owned_paths': f12},
        'PR550': {'merge': GLP334_SOURCE, 'accepted_head': analytics_head, 'owned_paths': analytics}}
    selected = sorted(set(f12) | set(analytics))
    if (declaration.get('schema') != 'glp334-receiving-union-v1'
            or declaration.get('payload_merge') != GLP334_SOURCE
            or declaration.get('authority') != 'SOURCE_ONLY_REQUIRES_SEPARATE_REVIEW_AND_RECEIVING_GRANT'
            or declaration.get('maintained_successor') != original['maintained_successor']
            or declaration.get('sources') != expected_sources
            or declaration.get('selected_paths') != selected or set(selected) != set(GLP334_PATHS)
            or len(f12) != 14 or len(analytics) != 11 or len(selected) != 17
            or declaration.get('preserved_destination_modes') != {'ops/glp/reporting-v2/source-manifest.json': 0o600}):
        raise RuntimeError('glp334_finite_receiving_union_moved')
    for merge, head, owned in ((GLP334_F12_SOURCE, f12_head, f12),
                               (GLP334_SOURCE, analytics_head, analytics)):
        for rel in owned:
            if git('ls-tree', merge, '--', rel) != git('ls-tree', head, '--', rel):
                raise RuntimeError('glp334_accepted_owned_identity_moved:' + rel)
    for rel in selected:
        owner = GLP334_SOURCE if rel in analytics else GLP334_F12_SOURCE
        if git('ls-tree', GLP334_SOURCE, '--', rel) != git('ls-tree', owner, '--', rel):
            raise RuntimeError('glp334_union_payload_moved:' + rel)
    original.update(selected_paths=selected,
                    preserved_destination_modes=declaration['preserved_destination_modes'],
                    payload_merge=GLP334_SOURCE, sources=expected_sources,
                    dependency_order=declaration['dependency_order'])
    return original


def enrollment_hashes(checkout):
    """Only the immutable reviewed payload, not newer main or caller hashes."""
    import hashlib
    import subprocess
    receiving_cohort(checkout)
    return {rel: hashlib.sha256(subprocess.check_output(
        ['git', '-C', str(checkout), 'show', GLP334_SOURCE + ':' + rel])).hexdigest()
            for rel in GLP334_PATHS}


def registered_job(job_id):
    if job_id in tuple(row[0] for row in GLP334_JOBS):
        return Job('GLP334', 'reporting-enrollment', 'A', GLP334_PATHS,
                   tuple(row[0] for row in GLP334_JOBS), tuple(row[1] for row in GLP334_JOBS), True)
    reporting = Job('GLP333', 'reporting', 'B', REPORTING_PATHS,
                    ('aadf6677c7f0',), ('glp_affiliate_weekly_reporting.sh',), True)
    retention = Job('GLP73', 'retention', 'A', ('scripts/reports_archiver.py',),
                    ('f3d2db07d83f', '69a28f878ce3'),
                    ('f12_generator_archiver.py', 'f13_growth_retention.py'), True)
    from discovery_predecessor_migration import COHORT
    discovery = Job('F01', 'discovery', 'A', tuple(sorted(COHORT)),
                    ('58202b06bdc0',), ('affiliate_supply_discovery_daily.sh',), False)
    aliases = {'glp333': reporting, 'glp-333': reporting, 'aadf6677c7f0': reporting,
               'glp44': replace(reporting, name='GLP44'),
               'glp-44': replace(reporting, name='GLP44'),
               'crontab:607efe4379607b3f': replace(reporting, name='GLP44'),
               'glp73': retention, 'glp-73': retention,
               'f3d2db07d83f': retention, '69a28f878ce3': retention,
               'f01': discovery, '58202b06bdc0': discovery}
    try:
        return aliases[job_id.lower()]
    except KeyError:
        raise RuntimeError('unregistered_ship_job:' + job_id) from None
