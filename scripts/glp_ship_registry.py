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


@dataclass(frozen=True)
class Job:
    name: str
    kind: str
    tier: str
    paths: tuple
    jobs: tuple
    wrappers: tuple
    window: bool


def registered_job(job_id):
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
