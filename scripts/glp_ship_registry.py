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


# Accepted PR554 payload; enrollment requires prior source receiving.
GLP334_SOURCE = 'b34c5de8fddcbd0bb8b9ecf1ebc04959064b4f4f'
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


def enrollment_hashes(checkout):
    """Only the immutable reviewed payload, not newer main or caller hashes."""
    import hashlib
    import subprocess
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
