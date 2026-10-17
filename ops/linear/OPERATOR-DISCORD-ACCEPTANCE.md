# Authenticated operator acceptance for protected closure

This is an alternative **human identity transport**, not an agent approval or authority for a new effect. The ordinary `linear_close.py` path still refuses protected effects. Use the existing typed `linear_orchestrator.py pass --protected-noncode` / `--protected-runtime` path; all original acceptance coverage, current description, runtime output hashes, legacy RESULT lint, installed provenance and exact-head CI checks remain intact. Separate clinical/design/release/send authorizations are not waived.

## Trust and binding

- Operator ID: `392067741500309517`, authenticated only by Discord GET through `post_discord_message.readback`. No JSON/file/chat copy is a substitute for that GET.
- Channel: #decisions `1524254375453786294`. Request bot: `1486880423568211968` (the existing poster). Never accept its own reply, any other bot, webhook, system, forwarded or edited message.
- Operator must **reply** to an unedited request from that bot in this channel, with exactly `ACCEPT GLP-n <first-eight-lowercase-hash-chars>` (or the full hash).
- The request must contain standalone `Evidence: reports/...`, `SHA256: <full hash>` (optional `• ` bullets), and `ACCEPT GLP-n <prefix>` lines. This v1 contract permits one primary runtime-evidence file per request; bundle multiple outputs in that file, do not drop evidence to fit the contract. Eight-character acceptance is valid only via this referenced, authenticated full-hash request.
- Freshness is authorization, not GET time: the human reply must be strictly newer than the primary evidence file's current mtime, no more than seven days old, and not future-dated. Rewriting/touching evidence requires a new human message even if its SHA256 is unchanged. The original exact issue/full-evidence-hash/path binding remains mandatory.
- Applying closure atomically consumes the channel/message ID in the host-wide durable SQLite ledger `/home/ubuntu/business/ops/linear/operator-acceptance-consumed.sqlite3`, **before any Linear write** (including startedAt). SQLite serializes contenders and commits with `synchronous=FULL`; the directory entry is fsynced. Capture/check-only only read the store. Reopening, an unconfirmed receipt, a failed Done write, or a process restart never releases a consumed message. A different message must also postdate the previous consumed attempt for that issue.
- The consumption ledger is retained authority, not a cache: preserve it across source upgrades, checkout changes, backups/restores and host migrations; never delete/reset it to retry. Closure is host-bound to this shared ledger, not supported against an independent empty ledger on another host. Unreadable/corrupt stores refuse. After uncertainty, inspect actual state/receipt before requesting a new human reply; a new reply is not permission to replay business effects.
- Both original snapshots, author/channel/message IDs, timestamps, full content, evidence path and full hash are retained. Any edit/deletion/mismatch fails a fresh GET at validation, repeated after provenance/lint checks. Parent separately binds the snapshot hash, the **entire** protected evidence payload and current description. FLEET_RESULT_V1 embeds the snapshots and binds the unchanged RESULT hash, rather than relying only on a mutable pointer.

## Coordinator procedure (not worker closure authority)

1. Prepare a genuine complete typed protected RESULT, retaining every original/nested acceptance clause and real evidence. Do not convert a simple worker RESULT by merely labelling it COMPLETE. Use the existing protected schema/legacy lint; this change does not remove those requirements.
2. After the operator's real reply, capture with the read-only helper:

   ```sh
   python3 scripts/linear_operator_acceptance.py GLP-125 /absolute/reports/path/RESULT.json OPERATOR_REPLY_ID
   ```

   Put the resulting object into `protected_review`. Never fabricate or edit a capture; both snapshots are fetched again by the validator.
3. Obtain a **new** authenticated coordinator acceptance on the exact Linear issue, with the existing prefix and standalone bindings:

   ```text
   Accepted description SHA256: <exact current description hash>
   Accepted protected evidence SHA256: <canonical RESULT hash excluding review/protected_review>
   Execution author Linear ID: <parent-attested execution actor UUID>
   Independent operator Discord acceptance SHA256: <canonical hash of protected_review.discord>
   ```

   Canonical hashing is SHA256 of UTF-8 `json.dumps(value, sort_keys=True, separators=(',', ':'))`. Use `linear_operator_acceptance.binding_sha256`. Bind the real coordinator comment ID and body hash in `review`; its existing same-hour requirement remains. Shared Linear identity is permitted **only** here because the second acceptance is the exact authenticated human operator in Discord.
4. Coordinator runs the existing `pass GLP-n /absolute/reports/path/RESULT.json --protected-noncode --check-only` (or `--protected-runtime`). Only an authorized coordinator may subsequently apply closure. A leaf must stop for independent Astra review; no merge/install/issue closure is authorized by this source change.

## Current request

Exactly one consolidated post was sent and read back via the existing poster: `1560838185641050233`. Only GLP-125 is ready: Gmail SENT `1a13037e99fe9c58`; `reports/ops/glp125-close-20261017/NUIMAGE-SENT-READBACK.json`; SHA256 `35dbc76e8cc43d936a74b1afbd2edc48c94606999d4762962edfed8a20b17501`. Requested reply: `ACCEPT GLP-125 35dbc76e`. GLP-320/327/328/47/250 were explicitly excluded as not ready. Posting the request is not acceptance.
