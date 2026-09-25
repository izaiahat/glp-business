#!/usr/bin/env python3
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Host Disk","output_class":"report","composer_type":"formatter","privacy_level":"internal","fixture":"⚙️ **Host Disk** — reclaimed **1.25 GiB** · root **70.0% used / 18.0 GiB free** · rehearsals **4→2** · builders **0**","fixture_output_class":"report","fixture_test":"/home/ubuntu/business/ops/tests/test_cron_discord_output_guard.py::test_registry_fixtures_lint","route":"1487300132054765629","detail_policy":"complete_summary_in_discord"}
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    # Domain RED is a successfully executed alert, not scheduler failure/retry.
    # All other failures retain nonzero scheduler semantics.
    sys.argv = [sys.argv[0], '--emit-noop', *sys.argv[1:]]
    try:
        runpy.run_path('/home/ubuntu/business/scripts/host_disk_pruner.py', run_name='__main__')
    except SystemExit as exc:
        raise SystemExit(0 if exc.code == 2 else exc.code)
