#!/usr/bin/env bash
set -euo pipefail

# An explicit fixture-only path; the default invocation remains production.
dry_run=0
if [[ "${1:-}" == "--dry-run" ]]; then
  dry_run=1
  shift
  fixture_root=''
  offline_output=''
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --fixture-root) [[ $# -ge 2 ]] || exit 2; fixture_root="$2"; shift 2 ;;
      --output-dir) [[ $# -ge 2 ]] || exit 2; offline_output="$2"; shift 2 ;;
      *) printf 'offline_argument_refused\n' >&2; exit 2 ;;
    esac
  done
  [[ -n "$fixture_root" && -n "$offline_output" ]] || { printf 'offline_fixture_and_output_required\n' >&2; exit 2; }
  fixture_root="$(realpath -e "$fixture_root")"
  for fixture_file in sheet-values.json tracking-links.json blockers.json backrun/coverage-receipt.json backrun/sheet-full-row-export.json backrun/marketplace/katalys-inventory.json; do
    [[ -f "$fixture_root/$fixture_file" ]] || { printf 'offline_fixture_missing:%s\n' "$fixture_file" >&2; exit 2; }
  done
  [[ -d "$fixture_root/captures" && -d "$fixture_root/probes" ]] || exit 2
  mkdir -p "$offline_output"
  offline_output="$(realpath -e "$offline_output")"
fi

payload='/home/ubuntu/business/scripts/glp_affiliate_reconciliation_honesty.py'
expected_payload_sha256='f91595edee8f8cc8d0fd0e31039cdc2fdc523fa333e4750b7d0daf3dc49d86d3'
actual_payload_sha256="$(sha256sum "$payload" | cut -d' ' -f1)"
if [[ "$actual_payload_sha256" != "$expected_payload_sha256" ]]; then
  printf '{"schema":"glp_affiliate_reconciliation_honesty_preflight_v1","status":"FAIL","reason":"payload_hash_drift","expected_sha256":"%s","actual_sha256":"%s"}\n' \
    "$expected_payload_sha256" "$actual_payload_sha256"
  exit 1
fi

# The rail scanner is a queue-only preflight and must still report when the
# protected live glp-design/deploy surface is changing under its own pin gate.
wire_scanner='/home/ubuntu/business/scripts/glp_wire_in_ready_queue.py'
wire_scanner_expected_sha256='a5c20feed5c2a8395d598fde28363d4a034682093629c5b90c7ab22dc984fcd1'
wire_rails='/home/ubuntu/business/scripts/glp_affiliate_link_rails.py'
wire_rails_expected_sha256='7a87021aee03c60215fb9d6eb85e9c7405812efbf14d488d65fd58b30c6c70d6'
wire_scope='/home/ubuntu/business/scripts/affiliate_provider_row_scope.py'
wire_scope_expected_sha256='4c2eb769723efbde2c7cb6fcb5f5bd7da2b236333dc1376098cd08056db4af90'
wire_writer='/home/ubuntu/business/scripts/affiliate_sheet_batch_writer.py'
wire_writer_expected_sha256='aef4747d1e9ca683df5bf0edc31c4be12d6769f28a9fe438f439f09e83bfefe3'
wire_metadata="/home/ubuntu/business/scripts/affiliate_provider_metadata.py"
wire_metadata_expected_sha256="04eb33474fc6a0d9791659f0bee8834ea430cbd1d5f37332bbbe06a5ab4be348"
for wire_path in "$wire_scanner" "$wire_rails" "$wire_scope" "$wire_writer" "$wire_metadata"; do
  wire_actual_sha256="$(sha256sum "$wire_path" | cut -d' ' -f1)"
  if [[ "$wire_path" == "$wire_scanner" ]]; then
    wire_expected_sha256="$wire_scanner_expected_sha256"
  elif [[ "$wire_path" == "$wire_scope" ]]; then
    wire_expected_sha256="$wire_scope_expected_sha256"
  elif [[ "$wire_path" == "$wire_metadata" ]]; then
    wire_expected_sha256="$wire_metadata_expected_sha256"
  elif [[ "$wire_path" == "$wire_writer" ]]; then
    wire_expected_sha256="$wire_writer_expected_sha256"
  else
    wire_expected_sha256="$wire_rails_expected_sha256"
  fi
  if [[ "$wire_actual_sha256" != "$wire_expected_sha256" ]]; then
    printf '{"schema":"glp.wire_in_ready_daily_reporting.v1","status":"FAIL","reason":"wire_scanner_hash_drift","path":"%s"}\n' "$wire_path"
    exit 1
  fi
done

# Q41 fit-first authority remains pinned until its natural drain is reconciled.
wire_fit_manifest='/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN/Q41/candidate/actual-evidence/FIT-MANIFEST-678.json'
wire_fit_manifest_sha256='7bb77dfded7f0c34c849f6a286d19cef17e377405f8176a4e02bc8af3b877a81'
if [[ "$(sha256sum "$wire_fit_manifest" | cut -d' ' -f1)" != "$wire_fit_manifest_sha256" ]]; then
  printf 'provider_fit_manifest_hash_drift\n' >&2
  exit 1
fi
pt_date="$(TZ=America/Los_Angeles date +%F)"
wire_report_dir="/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/wire-in-ready/daily/${pt_date}"
wire_args=(--apply --fit-manifest "$wire_fit_manifest" --captures /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine --additional-captures /home/ubuntu/business/reports/glp-affiliate-auto-reconciler/maxbounty-approval-chain --recursive-captures --fail-on-stuck)
if [[ $dry_run -eq 1 ]]; then
  wire_report_dir="$offline_output/scanner"
  wire_args=(--values "$fixture_root/sheet-values.json" --captures "$fixture_root/captures" --recursive-captures --fail-on-stuck)
  if [[ -d "$fixture_root/additional-captures" ]]; then
    wire_args+=(--additional-captures "$fixture_root/additional-captures")
  fi
fi
set +e
wire_output="$(timeout --kill-after=10s 600s python3 \
  "$wire_scanner" \
  "${wire_args[@]}" \
  --output "$wire_report_dir" 2>&1)"
wire_rc=$?
set -e
if [[ -n "$wire_output" ]]; then
  printf '%s\n' "$wire_output"
fi
if [[ $wire_rc -ne 0 && $wire_rc -ne 2 ]]; then
  printf '{"schema":"glp.wire_in_ready_daily_reporting.v1","status":"FAIL","reason":"wire_estate_scan_failed","rc":%d}\n' "$wire_rc"
  exit "$wire_rc"
fi
if [[ $dry_run -eq 1 && $wire_rc -ne 0 ]]; then exit "$wire_rc"; fi

declare -A b2_pins=(
  ['/home/ubuntu/business/scripts/katalys_approved_link_resolver.py']='46c0c8c259c25ab94ea9c1337b550b15569e7a01523f6da5d8a7260741fbd085'
  ['/home/ubuntu/business/scripts/katalys_generate_tracking_links.py']='7bad1d2ea2084004f98aefd85b5785f15771cebe61237d76c7884d1a293a2cd6'
  ['/home/ubuntu/business/scripts/affiliate_sheet_batch_writer.py']='aef4747d1e9ca683df5bf0edc31c4be12d6769f28a9fe438f439f09e83bfefe3'
  ['/home/ubuntu/business/scripts/glp_affiliate_wire_backlog_daily.py']='948a6c1bb6fb41fc8ad11ba4b9d236d5ea50823a1cff423fc3887012fe3c8b21'
  ['/home/ubuntu/business/scripts/glp_affiliate_wire_backlog_worker.py']='4e10f7d1557aaa33f7351ce97f5a1dcf9dc396bb4027ea8dfa8660fff2bb3ca2'
  ['/home/ubuntu/business/scripts/glp_provider_cardinality_test_refresh.py']='83cb9e73eeeeb184043ff9bf53f68265c22c51e104bb7aeb76eb9cae1e98d6aa'
  ['/home/ubuntu/business/scripts/glp_price_index_source_rebind.mjs']='5342eabc650b351b5d3c7a2dd1329cf013a7445dbc30260f5240f6606cae021c'
  ['/home/ubuntu/business/scripts/glp_provider_dynamic_profile_activation.mjs']='598a7d4c9b1a261471569e9c69df6ae45d6fb6572807f801ccd958f6f0acd5a0'
  ['/home/ubuntu/business/scripts/glp_go_runtime_manifest_refresh.mjs']='a2309d7e02b80f204b40011f0009cb7fcabaf046903dd09cfab085ccecd45e1e'
  ['/home/ubuntu/business/scripts/build_glp_affiliate_wirein_incremental_v2.py']='c5cef691ddaf5d5e80a9337ed2181a68b430e0808301707847d3efe98dc6b506'
  ['/home/ubuntu/business/scripts/glp_review_editorial_projection.py']='113ff2f339be09305a80d3e963e6f1f4395bf18218dec975f344ccd916ec72c8'
)
for b2_path in "${!b2_pins[@]}"; do
  actual_b2_sha256="$(sha256sum "$b2_path" | cut -d' ' -f1)"
  if [[ "$actual_b2_sha256" != "${b2_pins[$b2_path]}" ]]; then
    printf '{"schema":"glp_affiliate_reconciliation_honesty_preflight_v1","status":"FAIL","reason":"b2_payload_hash_drift","path":"%s"}\n' "$b2_path"
    exit 1
  fi
done

# Resolve every approved Katalys GLP row that is missing a provider-issued
# tracking link before the reconciliation agent plans the day's B2 slice. A
# provider-specific failure remains loud but does not suppress unrelated rows.
resolver_dir="/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/katalys-approved-link-resolution/${pt_date}"
# Keep ambiguous enqueue ownership across PT-day report directories.
resolver_args=(--apply-queue --queue-outbox-dir /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/katalys-approved-link-resolution/queue-outbox)
if [[ $dry_run -eq 1 ]]; then
  resolver_dir="$offline_output/resolver"
  resolver_args=(--backrun "$fixture_root/backrun" --offline-tracking-capture "$fixture_root/tracking-links.json")
fi
set +e
resolver_output="$(timeout --kill-after=10s 450s \
  /home/ubuntu/business/.venv-affiliate-blitz/bin/python \
  /home/ubuntu/business/scripts/katalys_approved_link_resolver.py \
  --output-dir "$resolver_dir" "${resolver_args[@]}" 2>&1)"
resolver_rc=$?
set -e
if [[ $resolver_rc -eq 124 ]]; then
  printf '{"schema":"katalys-approved-link-resolution-v1","status":"BLOCKED","reason":"resolver_timeout","loud_if_unresolved":true}\n'
elif [[ -n "$resolver_output" ]]; then
  printf '%s\n' "$resolver_output"
fi
if [[ $dry_run -eq 1 ]]; then
  [[ $resolver_rc -eq 0 ]] || exit "$resolver_rc"
  exec python3 "$payload" --probe-root "$fixture_root/probes" --blocker-store "$fixture_root/blockers.json"
fi
python3 "$payload" "$@"
exec python3 /home/ubuntu/business/scripts/glp_affiliate_wire_backlog_daily.py \
  --pt-date "$pt_date" \
  --output-dir "/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/wire-backlog-worker/daily/$pt_date" \
  --deploy
