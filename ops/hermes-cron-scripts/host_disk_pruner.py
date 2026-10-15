#!/usr/bin/env python3
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Host Disk","output_class":"report","composer_type":"formatter","privacy_level":"internal","fixture":"⚙️ **Host Disk** — launched **host-disk-cleanup.service** · detached full backlog pass · completion receipt and reclaimed GiB will be posted by the unit","fixture_output_class":"report","fixture_test":"/home/ubuntu/business/ops/tests/test_cron_discord_output_guard.py::test_registry_fixtures_lint","route":"1487300132054765629","detail_policy":"complete_summary_in_discord"}
import runpy
import sys

if __name__=='__main__':
    # Dry-run remains available through the real entrypoint without launching a unit.
    if '--dry-run' in sys.argv:
        runpy.run_path('/home/ubuntu/business/scripts/host_disk_pruner.py',run_name='__main__')
    else:
        runpy.run_path('/home/ubuntu/business/scripts/host_disk_pruner_trigger.py',run_name='__main__')
