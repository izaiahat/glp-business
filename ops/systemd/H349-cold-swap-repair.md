# H349 cold swap page-in repair

The host's registered cleaner f33f1b27fde6 forced 2.5–2.7 GiB of live cold
swap into memory three times on 2026-10-11/12. Those receipts prove maintenance
I/O, not that the cleaner caused U1's 01:49 UTC refusal (it did not rotate then).
A low two-second PSI/swap sample cannot establish that cold heap is garbage.

This change retires automatic swap rotation completely. Literal-PID orphan
reaping and protected CDP/default/operator browser vetoes are unchanged.
No delegation-admission threshold or cgroup limit changes. Existing `--all-files`
is accepted as a no-effect compatibility option. Pressure receipts remain real.

The preceding commit retains the exact installed source/test/launcher bytes for
reviewed-baseline adoption. The tests import the selected checkout, not live code.
The regression runs real `main` with only effects mocked; it failed on the old
source because `sudo swapoff` was called and passes on the repaired source.

After independent safety review and green exact-head CI, install only the cleaner
source, its test, and curated launcher with `sync_from_repo.py` dry/apply/zero-diff.
For the original source/test, `--adopt-reviewed-baseline` binds to the preceding
commit. Atomically replace only the matching native Python launcher, refresh
only its script hash through supported `cron.jobs.update_job`, preserve all
schedule/output/lifecycle fields, then run the exact registered script manually.
Do not edit a running shell script or touch the disk pruner.

This repair does not meet the remaining state.db <8 GB acceptance. Supported
storage v3 migration/compaction refuses live foreign DB/WAL/SHM holders. Current
60-day retention has no candidates; never change retention, force maintenance,
or interrupt the coordinator/gateway to make an acceptance number.
