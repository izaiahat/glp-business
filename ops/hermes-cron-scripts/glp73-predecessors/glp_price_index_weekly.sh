#!/usr/bin/env bash
# Weekly public GLP-1 Price Index regeneration and standard release chain.
set -euo pipefail
umask 077
: <<'CRON_DECLARATION'
CRON_DISCORD_HOUSE_STYLE_V1
CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP-1 Price Index","output_class":"report","composer_type":"no_agent_script_discord_fmt","privacy_level":"internal","fixture":"✅ **GLP-1 Price Index** — weekly observation is live through the standard release chain\n\nobserved: **2026-08-10** · deployment: **00000000-0000-0000-0000-000000000000** · sensor: **PASS** · IndexNow: **PASS_ACCEPTED**\n→ regenerated from tracker lineage through candidate → deploy → remint → sensor → IndexNow\n-# receipt: /home/ubuntu/business/reports/glp-clinic-scout/growth/price-index/runs/20260810T132000Z/chain-receipt.json","fixture_output_class":"report","fixture_test":"python3 -m unittest discover -s tests -p 'test_glp_price_index_growth.py' -v","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":false,"schedule":"20 6 * * 1 America/Los_Angeles","standard_chain":"candidate -> deploy -> remint -> sensor -> indexnow"}
CRON_DECLARATION
cd /home/ubuntu/business
# Keep nested Node test subprocesses within this host's thread budget. Without
# this bound, spawnSync can abort under concurrent cron load and return a null
# status even though the same exact test files pass in isolation.
export UV_THREADPOOL_SIZE=1
export GOMAXPROCS=1
payload=/home/ubuntu/business/scripts/glp_price_index_weekly_successor.py
payload_expected_sha256="d70510d518021e1da9f5a8987b2b11ff0942425920aaaa5c036d3879fb91f241"
materializer=/home/ubuntu/business/scripts/site_tree_materializer.py
materializer_expected_sha256="f225d17a5cc1683be33457d9e4f56057b1789da91b59cb81a95b042d06573de4"
builder=/home/ubuntu/business/systems/seo-blog-machine/scripts/build_opus_article_release.py
builder_expected_sha256="5398096b79f89556ce26a93c3a10160a5fdd329ec67a6993b9d5475eb688e287"
payload_actual_sha256="$(sha256sum "$payload" | cut -d' ' -f1)"
materializer_actual_sha256="$(sha256sum "$materializer" | cut -d' ' -f1)"
builder_actual_sha256="$(sha256sum "$builder" | cut -d' ' -f1)"
if [[ "$payload_actual_sha256" != "$payload_expected_sha256" || "$materializer_actual_sha256" != "$materializer_expected_sha256" || "$builder_actual_sha256" != "$builder_expected_sha256" ]]; then
  printf '⚠️ **GLP-1 Price Index** — control pin drift; weekly release chain not started\n\n→ re-pin the generator, COW materializer, and release builder together, then re-register this wrapper\n-# external effects: 0\n'
  exit 1
fi
test ! -L /home/ubuntu/business/ops/claude_model_policy.py && test "$(sha256sum /home/ubuntu/business/ops/claude_model_policy.py | cut -d' ' -f1)" = "06d925636b5d25e21ed2e1ed34184cb482d40400e10ae1869fd3eb4b24f8f8e4" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/ops/discord_fmt.py && test "$(sha256sum /home/ubuntu/business/ops/discord_fmt.py | cut -d' ' -f1)" = "e6e0ed7231282b2f222a435d8879ca22cfe2317900f6cbb122f48a83e9263acc" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/ops/glp/seo_business_context.py && test "$(sha256sum /home/ubuntu/business/ops/glp/seo_business_context.py | cut -d' ' -f1)" = "ba5d96812d06c08fb828bb87523894f8898f21a1ad8e58fffb3450c66d54ea44" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/ops/glp/seo_data_rails.py && test "$(sha256sum /home/ubuntu/business/ops/glp/seo_data_rails.py | cut -d' ' -f1)" = "5b2ad3984df46ef1d66c855f0fb23ca6b3780f51223c45e88a044be854c20386" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_bing_fast_index.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_bing_fast_index.py | cut -d' ' -f1)" = "63780813af20a5670fd7587484f98dc23dc6c60376d6b308539c26c8b1f11017" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_current_content_base_schema.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_current_content_base_schema.py | cut -d' ' -f1)" = "a2a65183b8f501213e9f03730a64101e141c79c43035009a7bf7b28c0a676e46" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_deploy_completeness_sensor.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_deploy_completeness_sensor.py | cut -d' ' -f1)" = "3beabf18de1c04f08bed54d638626c4aabd842ce417a85bab969a2079ae1d99c" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_deploy_sensor_hook.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_deploy_sensor_hook.py | cut -d' ' -f1)" = "5e8b2cb133a476a6b42fe2ff0ce0fd7bb21100563e783532022b5d246c7323fb" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_indexnow_changed_set.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_indexnow_changed_set.py | cut -d' ' -f1)" = "19395be174d77c1bb39f1b020c8d9dc137ecfc0f57ae59db5f92c30723c4c668" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_llms_txt_gate.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_llms_txt_gate.py | cut -d' ' -f1)" = "85885f37baafd09494439a3108fdaa11148170a82d327fb04694ef448c5b0666" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_price_index_recovery.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_price_index_recovery.py | cut -d' ' -f1)" = "20d84ddc3f27eb18aba86de11d80177ca66ad46b5059ca3ed816beac04300ef9" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_price_index_weekly.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_price_index_weekly.py | cut -d' ' -f1)" = "0f5d99182b376526fb6ff4fbf420c0c9c3868996359127c367729bd31f235105" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_price_index_weekly_successor.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_price_index_weekly_successor.py | cut -d' ' -f1)" = "d70510d518021e1da9f5a8987b2b11ff0942425920aaaa5c036d3879fb91f241" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_robots_gate.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_robots_gate.py | cut -d' ' -f1)" = "8086732046b456893c7c5757171a6f86f21c337b6df3d59f35d3ed71b0ffa3e1" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_served_test_contract.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_served_test_contract.py | cut -d' ' -f1)" = "7b08da73ffed67dbac6545aa14ced5fdb67b6167797b017bd6b4fe08611f1234" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/glp_sitemap_lastmod_gate.py && test "$(sha256sum /home/ubuntu/business/scripts/glp_sitemap_lastmod_gate.py | cut -d' ' -f1)" = "061c01d7cf60903040d929bde9b7ee0e1521ee4ec5ad5b9d0a242913440b3321" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/scripts/site_tree_materializer.py && test "$(sha256sum /home/ubuntu/business/scripts/site_tree_materializer.py | cut -d' ' -f1)" = "f225d17a5cc1683be33457d9e4f56057b1789da91b59cb81a95b042d06573de4" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/accepted_failure_contract.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/accepted_failure_contract.py | cut -d' ' -f1)" = "42b8b2c82c6663cca3aa9d57f05c526a9046f880874a1f404ad8e3e76b79463e" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/analytics_feedback.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/analytics_feedback.py | cut -d' ' -f1)" = "b856ffdfd94f395333b56f7f6d6401fbbda1ff137a680907144919c0d652f74f" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/article_image_release_contract.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/article_image_release_contract.py | cut -d' ' -f1)" = "911aaabbd62bc901a420cb8eae2c4cb2edac52834fc9b067720a44ae846297fa" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/article_images.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/article_images.py | cut -d' ' -f1)" = "76f1582c0cd3d0fb652bc9760fd21dc9561da562f77781887ac5c2dbb67a2f3a" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/build_opus_article_release.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/build_opus_article_release.py | cut -d' ' -f1)" = "5398096b79f89556ce26a93c3a10160a5fdd329ec67a6993b9d5475eb688e287" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/carrier_fixture_contracts.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/carrier_fixture_contracts.py | cut -d' ' -f1)" = "125a4b5b2067074a9f21f0aac2ac98145c3e3b9e6bfa3ac06b5585b4353007e9" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/carrier_lock.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/carrier_lock.py | cut -d' ' -f1)" = "10e90004795c150b1a459f78b34b990d96494cf75c923e35dfd33e348ca4c9cf" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/current_content_base.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/current_content_base.py | cut -d' ' -f1)" = "841e7f4330f6ee435d3b0f1ab551cf789bc2f0b3e5cf47deca4190c2ca98588c" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/defect_remediation.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/defect_remediation.py | cut -d' ' -f1)" = "dcbf7dd9094e1bbd1dc9c46fea6183e6713aea9e2b7424f78b75f116f9ff6abe" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/deterministic_qa_repair.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/deterministic_qa_repair.py | cut -d' ' -f1)" = "10b2e9f76c386839917da5760b23fc013007ce8657ed8fbf1d236aa83ac1b380" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/existing_article_updates.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/existing_article_updates.py | cut -d' ' -f1)" = "14c1384dbf6e536dbf0b25fc2b08b867034ca18d64dd3f7121944aa5b6988144" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/gpt_image_2_native.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/gpt_image_2_native.py | cut -d' ' -f1)" = "3ed62995e892457a5123fd48c361cad6d76253f701d5f934f69428b91dc35d18" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/machine_config.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/machine_config.py | cut -d' ' -f1)" = "76d04a30fdca5183f7788cc9f29b1408999cd5f9fb9a3d09b59392c5489fe0d4" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/materialize_opus_run.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/materialize_opus_run.py | cut -d' ' -f1)" = "1b6600045de75c1600b7b0fe11f93e128318beb6e657d4b69d572f654fd0a165" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/r2_execution_control.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/r2_execution_control.py | cut -d' ' -f1)" = "eea97573c0c269aa6092b57300bb86fe196d00c28b5181e5f080f476d53f93cc" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/r2_update_carrier.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/r2_update_carrier.py | cut -d' ' -f1)" = "5b831b9f3318b97b892d96700a62a9684c2844555ff1b7af04bfa6460bf37d71" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/remint_content_base.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/remint_content_base.py | cut -d' ' -f1)" = "020eee1b281e9638b0e74b1f8f46ff32b30dfd6b342c2ff78ce27c48f2438b35" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/review_transports.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/review_transports.py | cut -d' ' -f1)" = "021f6bc70925160b88a1a71d3bb3f7162e203360bfc99a85e16cec712203d1ad" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/selection_receipt.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/selection_receipt.py | cut -d' ' -f1)" = "ab0f9a18484fdb3c4e62c3b7e835cf4aa63954f8a79fec7a5c7a68be12eac301" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
test ! -L /home/ubuntu/business/systems/seo-blog-machine/scripts/topic_feed.py && test "$(sha256sum /home/ubuntu/business/systems/seo-blog-machine/scripts/topic_feed.py | cut -d' ' -f1)" = "bc1691c3934dd3b0dd0cc1e926a05b08ecd945696e927c765350077eadc1bd50" || { printf 'Recovery source pin drift; no recovery started\n'; exit 1; }
set +e
OUT=$(python3 /home/ubuntu/business/scripts/glp_price_index_weekly_successor.py)
CODE=$?
set -e
python3 - "$OUT" "$CODE" <<'PY'
import json, sys
sys.path.insert(0, "/home/ubuntu/business/ops")
import discord_fmt as fmt
try:
    result = json.loads(sys.argv[1] or "{}")
except ValueError:
    result = {}
code = int(sys.argv[2])
def nonblank(value, fallback):
    text = str(value if value is not None else "").strip()
    return text or fallback

receipt = nonblank(result.get("receipt"), "/home/ubuntu/business/reports/glp-clinic-scout/growth/price-index/runs/unknown/chain-receipt.json")
if code == 0 and result.get("status") == "PASS":
    print(fmt.render(
        fmt.title("ok", "GLP-1 Price Index", "weekly observation is live through the standard release chain"),
        fmt.kvline(("observed", nonblank(result.get("observed_on"), "unknown")), ("deployment", nonblank(result.get("deployment_id"), "unknown")), ("sensor", nonblank(result.get("sensor_status"), "unknown")), ("IndexNow", nonblank(result.get("indexnow_status"), "unknown"))),
        fmt.meaning("regenerated from tracker lineage through candidate → deploy → remint → sensor → IndexNow"),
        fmt.subtext(f"receipt: {receipt}"),
        output_class="report"))
else:
    raw_blockers = result.get("blockers")
    candidates = raw_blockers if isinstance(raw_blockers, (list, tuple)) else [raw_blockers]
    blocker = next(
        (str(value).strip() for value in candidates if value is not None and str(value).strip()),
        "release chain returned no structured blocker",
    )
    print(fmt.render(
        fmt.title("action", "GLP-1 Price Index", "weekly release chain is blocked or failed"),
        fmt.kvline(("status", nonblank(result.get("status"), "UNKNOWN")), ("sensor", nonblank(result.get("sensor_status"), "not run"))),
        fmt.section("Blocker", blocker[:500]),
        fmt.meaning("inspect the private receipt before retrying; do not bypass candidate, remint, sensor, or IndexNow gates"),
        fmt.subtext(f"receipt: {receipt}"),
        output_class="alert"))
PY
exit "$CODE"
