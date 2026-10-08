# F10 / GLP-349 host safety repair

## Deterministic kael-audit producer

The legacy root system unit invokes `/home/auditbot/audit/run_audit.sh`, which runs Claude Opus,
background autofixes, a QA Python script, and an email sender. Its 30-minute timeout kills
that whole chain and the most recent report is stale. This PR replaces only its producer
with a bounded local observation command. It performs no process signalling, pruning,
network access, publication, remediation, or third-party sends. The original timer stays
unchanged. Exit zero means **fresh report produced**; `assessment: degraded` is retained
when the host fails checks, and is never represented as a green health result.

After exact-head CI and one independent Astra review, merge/install via the existing
`sync_from_repo.py --dry-run`, `--apply --wait 900`, `--dry-run` path (restricted to this script).
Then create `/home/ubuntu/business/reports/host-health-audit` owned by ubuntu, and install
`ops/systemd/kael-audit-host-health.conf` as
`/etc/systemd/system/kael-audit.service.d/10-host-health.conf`. Preserve the original unit and
existing `override.conf`; do not overwrite either. `sudo systemctl daemon-reload`, then
`sudo systemctl start kael-audit.service`. Read `LATEST.json`, actual ExecMainStatus,
Result, and `kael-audit.timer` next trigger. New report root is explicitly
`/home/ubuntu/business/reports/host-health-audit`; old reports stay untouched.

## Pruner and weekly archive

The scheduled legacy session pruner remains disabled because it still performs manual
SQLite maintenance and pauses live Hermes processes. It must not be re-enabled under
this assignment's no-interruption rules. Native `sessions.auto_prune=true`, retention 60
is already present, but there is currently insufficient local headroom for a consistent
backup plus compaction. No raw SQL delete/VACUUM, process pause, gateway restart, or
profile-wide sweep is authorized. Obtain disk headroom through the registered evidence-
preserving cleanup first; then back up before supported `hermes sessions prune` or
`hermes sessions optimize-storage --yes`, preserving active/pinned sessions. Manual
support for automatic retention is not evidence of a successful next scheduled run.

The weekly archive wrapper refuses because hashes of `disk_retention.py` and
`host_disk_pruner.py` no longer match its old pin manifest. Do not blindly repin it:
its controller also requires a quiet host and archives by pausing/restoring processes.
Retire that scheduler entry under this task's explicit 'fix or retire' authority;
keep its guarded source, historical receipts, and existing archives. Replacement offsite
backup work belongs to GLP-110 after a supported non-disruptive path is installed.
The social `engagement_engine` crontab entry is already commented out and its script
no longer exists. Retain this retirement: it is an unmaintained external-send path,
not a host housekeeping job. Do not revive it merely to clear a stale sensor warning.

## Pyright process and capacity

PID 2817899 was `pyright-langserver --stdio` below the coordinator's Hermes TUI gateway,
not a browser, publisher, deployer, or Hermes process. It had only stdio/event-loop FDs,
no sockets, open workspace files or child processes. Literal-PID SIGTERM retired that
stateless diagnostic process; the coordinator/gateway stay live. Saved default-profile
`lsp.servers.pyright.env.NODE_OPTIONS=--max-old-space-size=1536` bounds **fresh** LSP services.
The already-running coordinator cached its original LSP settings: its replacement server
still inherits an 8192-MiB Node cap. Do not claim the saved cap is adopted there, or restart
that coordinator while it has live workers. Bound/adopt it via supported in-process LSP
reset or a normal session handoff after children exit; no TUI input injection.

Acceptance still requires the parent-owned 8-worker, 24-hour workload observation.
A leaf worker cannot dispatch 8 workers, and a short PSI snapshot cannot satisfy 24 hours.

## Safety incident

A legacy backup script invoked with `--help` did not parse args and executed its main,
briefly stopping/restarting the gateway before failing (~3.5 seconds, zero backup).
All paused processes were resumed, gateway returned active, and coordinator PID 1565306
remained live. The incident is recorded in Linear comment 9e1577ac-5444-4791-b719-ca0f1037d30d.
Read entrypoints before invoking help on legacy scripts; never use this backup path again.
