# Rook existing-adapter launcher registration

`ops/rook-mcp/rook_adapter_launch.py` is the maintained source of
`/home/ubuntu/.hermes/scripts/rook_adapter_launch.py`, registered by the existing
user unit `rook-mcp-adapter.service`. The unit, interpreter, identities, flags,
private store, lock and fresh-channel-cursor behavior are unchanged.

The launcher imports and execs the supported installed source at
`/home/ubuntu/business/scripts/rook_mcp_events.py`; disposable Git worktrees
must not be runtime dependencies. This source repair does not adopt newer
adapter code, change OAuth/Host/Origin policy or renew listener/request authority.

For an explicitly authorized restoration after independent exact-head acceptance:

1. Authenticate the installed adapter and `receive_one_discord_message.py` against
   their imported sync baselines and real reviewed Git objects. Import the module
   with the existing `/home/ubuntu/.hermes/venvs/rook-mcp/bin/python -B` only; do not
   call its launcher, open Store or install test dependencies during preflight.
2. Use `scripts/sync_from_repo.py --checkout <accepted-merged-checkout> --only
   ops/rook-mcp/rook_adapter_launch.py` first with `--dry-run`, then with `--apply
   --wait 900`, then dry-run again (diffs=0). Sequence the shared baseline write
   through the parent. Do not select the adapter or blanket-import main.
3. Preserve the exact old installed launcher privately. Publish the scoped
   installed maintained launcher to its existing registered destination by an
   owner-only, same-directory atomic replacement, checking the old byte preimage
   immediately before replacement. Require reviewed-byte equality afterward;
   no symlinks or reconstruction of the vanished checkout.
4. Reconcile all sending/uncertain requests, operations and attempted deliveries
   read-only. With the existing unit failed/MainPID0 and no ambiguous effect,
   restart **only** `rook-mcp-adapter.service`. Do not restart the tunnel,
   presence, gateway, TUI or browser. Startup intentionally skips downtime
   messages using a genuinely fresh exact-channel cursor; never replay them.
5. Require running/nonzero PID, loopback18796 listening, protected-resource
   metadata HTTP200 and unauthenticated MCP POST401. Existing tunnel `/health`
   must independently report ready=true. These checks prove host readiness,
   not provider listener health.
6. Reconcile existing native task `6ac6da0a1e588191a433d1fa4c35bc4c` paused and
   original binding before one bounded native Resume. Keep any ambiguous result
   unresolved; do not retry, create listeners, replay internal APIs or edit DB.
   Verify a genuine matching lease and installed dispatcher gate before one
   admitted read-only GLP-359 ticket. Parent owns admission and closure.

Paid Pro entitlement, MCP listener lease and per-request reply authority are
three different objects. Restoring a host import does not renew any of them.
