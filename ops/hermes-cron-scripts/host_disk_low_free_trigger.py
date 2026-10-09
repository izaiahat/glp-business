#!/usr/bin/env python3
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Host Disk","output_class":"alert","composer_type":"formatter","privacy_level":"internal","fixture":"⚠️ **Host Disk** — root below 15% free\n\nroot **13.2% free** · launched **host-disk-cleanup.service** low-disk pass\n→ no action needed — the unit posts reclaimed GiB when it finishes","fixture_output_class":"alert","fixture_test":"/home/ubuntu/business/tests/test_host_disk_auto_classes.py::test_low_free_trigger_launches_only_below_threshold","route":"1487300132054765629","detail_policy":"complete_summary_in_discord"}
"""Cheap no-agent low-disk trigger: below 15% root free, launch the registered cleanup unit.

Silent (empty stdout) when free space is healthy or the unit is already running.
"""
import os
import runpy

THRESHOLD = 0.15
TRIGGER = '/home/ubuntu/business/scripts/host_disk_pruner_trigger.py'


def free_fraction(path='/'):
    d = os.statvfs(path)
    return d.f_bavail / d.f_blocks


def main(trigger_path=TRIGGER, fraction=None):
    fraction = free_fraction() if fraction is None else fraction
    if fraction >= THRESHOLD:
        return ''
    result = runpy.run_path(trigger_path)['trigger']()
    if not result['launched']:
        return ''
    return (f"⚠️ **Host Disk** — root below 15% free\n\n"
            f"root **{fraction*100:.1f}% free** · launched **{result['unit']}** low-disk pass\n"
            "→ no action needed — the unit posts reclaimed GiB when it finishes")


if __name__ == '__main__':
    message = main()
    if message:
        print(message)
