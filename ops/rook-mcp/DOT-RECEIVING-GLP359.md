# GLP-359 receiving repair — parent/reviewer handoff, not live acceptance

This change is source-only. A distinct Astra review of the exact PR head is
required before merge/install/live migration, expiry or consumption. Parent owns
admission, rollout, receipt disposition and GLP-359 closure. No new Cloud request,
Discord send, connector/VPN/ACL/credential change or denial acceptance is needed
or authorized by this repair. Do not weaken Finch confinement to run it: the
parent's existing native filesystem access is distinct from the adapter sandbox.

## Exact pending originals

- Finch: `finch-glp-359-6d7662314537ae6b`, request
  `1559692339130146898`, confirmed reply `1559692667938668654`.
  Full-wire SHA256:
  `4ff073cfdbcb814a509c37d1395b687bd6d28f7dc9a3c92cc06d8302447ac656`.
- Its single terminal attachment registration (no URL input):
  ```json
  {"id":"1559692667322114129","filename":"glp359-existing-probe-evidence.txt","content_type":"text/plain","byte_length":4647,"sha256":"0a4b1c9d3c19d2e375196f49b4be87438f61b6656d9c01cd0c9f78993b085524"}
  ```
  Provenance is **assistant-copy**, NOT a direct execution-tool export. The
  receipt stages original UTF-8 bytes and native attachment custody durably in
  private SQLite before stdout. REPLY.md retains the attachment text and custody.
  Replay verifies saved bytes/custody without another GET. Omitted opt-in keeps
  legacy registration digests and the existing default attachment refusal.
- Rook: `rook-glp-359-65f335b45f9a8530`, request `1559649536497156118`,
  external reply `1559653286296952912`, full-wire SHA256
  `886c8101dcb343a34c16a1242b2f5ea476fc7260d49184fbb47e84190c0be806`.
  Default eligible cutoff remains **2026-10-14T01:30:26.207393Z**. External
  expiry creates `DOT_EXTERNAL_HANDOFF_EXPIRY_V1`, not REPLY.md, COMPLETION.json
  or a native confirmation. The original body and reference are retained.

## Reviewed Finch migration, only after review and parent authorization

Use installed reviewed source, not a sandbox workaround. First read fresh exact
TICKET.json, CONFIG.json, NATIVE-CONFIG.json, the readonly native request and
receipt DB. Preserve the two already-completed Finch tickets and three OTHER
uncertain Rook operations; those operations prevent a global no-inflight claim.

The explicit administrative interface is:

```sh
<approved-parent-python> /home/ubuntu/business/scripts/dot_dispatch.py \
  --migrate-receiving /home/ubuntu/business/reports/ops/dot-tickets/finch-glp-359-6d7662314537ae6b \
  --receiving-migration-plan <absolute-private-reviewed-plan.json>
```

The plan is an owned, nonsymlink 0600 JSON file with **exactly** these fields:

| Field | Required value/source |
|---|---|
| schema | `DOT_RECEIVING_MIGRATION_V1` |
| ticket | Exact Finch ticket above |
| ticket_preimage_sha256 | SHA256 of original TICKET.json UTF-8 bytes, including whitespace |
| native_config_preimage_sha256 | SHA256 of original NATIVE-CONFIG.json bytes |
| native_row_sha256 | SHA256 of `dot_dispatch.canonical(native_reply_consumer.native_row(old_registration))` |
| old_source_pins | Exact original ticket source_pins object |
| new_source_pins | Reviewed approved `dot_dispatch.source_pins()` from the actual installed source |
| reply_id | Exact Finch reply above |
| content_sha256 | Exact Finch full-wire SHA above |
| terminal_attachment | Exact five-field object above |
| recorded_by | Actual parent owner (NAME syntax); not a forged operator approval |
| reason | 1–1000 characters naming exact PR/head and independent review receipt |

No plan was applied by DOT-FIX. Do not hand-edit the config or repin globally.
The migration checks exact native/message policy/authentication before writing,
locks dispatch and receipts, refuses ANY incumbent receipt/completion/expiry,
and preserves exact original TICKET.json/NATIVE-CONFIG.json text in
RECEIVING-MIGRATION.json before replacing the two files. Receipt identity,
correlation, request ID, parent/task owner, old registration digest, sent_at and
old/new pins remain bound. Native DB is read-only. Audit-before-write recovery
admits only the same reviewed plan and its exact original/replacement bytes;
a changed native row or partial-write conflict holds rather than inventing work.
Ordinary consume never invokes this migration.

After successful authorized migration, parent may replay the existing Finch
completion through the registered dispatcher. Check actual COMPLETION.json,
REPLY.md and the staged receipt; parent `received` and task-owner `applied` or
`rejected` remain distinct later actions. No duplicate request is needed.

## Reviewed Rook external expiry, only after unchanged cutoff

After independent review, parent must freshly classify the three OTHER uncertain
Rook operations and reconcile effects before choosing to expire this ticket.
Keep timeout at its default; the exact-hash alternative refuses any changed age.
The parent-authorized call uses both exact reply ID and full-wire hash:

```sh
<approved-parent-python> /home/ubuntu/business/scripts/dot_dispatch.py \
  --expire /home/ubuntu/business/reports/ops/dot-tickets/rook-glp-359-65f335b45f9a8530 \
  --owner <actual-parent-owner> --reason <reviewed-external-expiry-reason> \
  --external-handoff-id 1559653286296952912 \
  --external-handoff-sha256 886c8101dcb343a34c16a1242b2f5ea476fc7260d49184fbb47e84190c0be806
```

Both native probes must show expired request custody. Exact authenticated
request readback, complete bounded history, unique external reply, author,
channel/guild, full-wire hash, reply type/reference, no webhook/attachment, and
second exact GET equality remain mandatory. The legacy DIRECT FALLBACK RESULT
branch remains supported; a free-form RESULT without the reviewed hash does not.
Incomplete history or any native confirmation/staged/sending/uncertain effect
refuses, without resetting cursors, cutting grants short or resending work.

## Verification and independent review focus

Owning suites (network/credentials denied, synthetic fixtures):

```sh
<isolated-python> -B -m pytest -q -p no:cacheprovider \
  tests/test_native_reply_consumer.py tests/test_dot_dispatch.py \
  tests/test_dot_dispatch_recovery.py
```

The existing native-reply-consumer and dot-lane hosted workflows select these
owning files at the PR head. Finch confinement/adapter regressions stay selected
by the existing finch-file-review workflow; no CI or security gate was removed.
Review opt-in/default digest stability, URL/effect custody, downloaded-byte hash,
saved replay tamper detection, deadline/crash staging, source-pin/preimage and
receipt conflicts, lock/race guards, audit-only interrupted migration, exact
external reference/hash/history and unchanged cutoff. Offline clock/history/CDN
substitution is not live expiry/receiving proof.

The source reproduction inputs remain at the absolute paths
`/home/ubuntu/business/lanes/DOT-RECEIVE/{RESULT.json,REPAIR-HANDOFF.md,reproduce.py,REPRO.json,AUTHENTICATED-READBACK.json}`
and `/home/ubuntu/business/lanes/DOT-CLOUD-FINAL`. The 23-assertion reproducer
exercises **installed predecessor** behavior, not candidate acceptance. Its
safe_message projection intentionally omits signed CDN URLs: an exact fresh
Discord metadata GET is required for real URL-custody verification; do not
reconstruct a URL or bypass its native hash.

## Separate unchanged pilot holds

A received assistant copy does not establish account/workspace/executed-version
join, canonical task URL, body nonce or an enforced HEAD denial. Envoy/503 is not
a policy-denial witness. Preserve PR469 fail-closed witness/refusal behavior,
off-host workload proof and independent Fable acceptance. This PR does not
accept the ConcreteCloud pilot or make all-machine-live/root acceptance true.
