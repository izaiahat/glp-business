"""Publication-day contract across the real packet compiler, builder, renderer and publisher.

No live packet or deployment is used. Dates are historical/hypothetical fixtures.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "systems/seo-blog-machine/scripts"
RENDERER = ROOT / "site-system/flagship-v2/work/glp215-accepted-base/scripts/build-staging.mjs"


def module(name):
    # These imports are host-only policy/pointer modules, not the date logic.
    # Stub their import boundary so CI cannot accidentally consume live files.
    stubs = {
        "decision_policy": ("consumer_policy", "require_allowed", "write_consumer_decision"),
        "glp_current_content_base_schema": ("COUNT_KEYS", "POINTER_REQUIRED_KEYS", "POINTER_SCHEMA",
                                            "POINTER_STATUS", "validate_pointer_shape", "validate_supersedes"),
        "glp_price_index_weekly": ("_validate_base_evidence",),
        "autonomous_release_gate": (),
    }
    for key, fields in stubs.items():
        stub = ModuleType(key)
        for field in fields:
            setattr(stub, field, set() if field in {"COUNT_KEYS", "POINTER_REQUIRED_KEYS"} else
                    (lambda *args, **kwargs: None))
        sys.modules[key] = stub
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def render(article):
    # Exercise the registered renderer's real addGuideMetadata function, not a
    # duplicate schema mapping. Dependencies irrelevant to dates are isolated.
    source = RENDERER.read_text()
    fn = source[source.index("function addGuideMetadata("):source.index("\nfunction addQuizBreadcrumbSchema(")]
    runner = '''
const vm = require('node:vm'), fs = require('node:fs');
const article = JSON.parse(process.argv[1]);
const context = {buildSchemaGraph: args => ({jsonLd: args}), existsSync: () => false,
  path: require('node:path'), ROOT: '/unused', escHtml: x => x};
vm.createContext(context);
vm.runInContext(fs.readFileSync(0, 'utf8') + '\\nthis.render = addGuideMetadata;', context);
const html = context.render('<html><head></head><body></body></html>', article, {});
console.log(html.match(/<script type="application\\/ld\\+json">([^<]+)<\\/script>/)[1]);
'''
    output = subprocess.check_output(["node", "-e", runner, json.dumps(article)], input=fn, text=True)
    return json.loads(output)


def packet(tmp_path):
    meta = {
        "slug": "/guides/date-binding-fixture/", "primaryKeyword": "fixture guide",
        "title": "Fixture guide", "h1": "Fixture guide", "metaDescription": "Fixture description",
        "datePublished": "2026-09-05", "dateModified": "2026-09-05",
        "pricesCheckedDate": "2026-09-05", "captureContract": {
            "action": "/api/content-upgrade", "consentVersion": "glp-content-upgrade-v2-20260801",
            "sourcePage": "guides-date-binding-fixture", "leadMagnetKey": "guides-date-binding-fixture-terms-checklist",
            "emailField": "email", "healthDataFields": [],
        },
    }
    import yaml
    (tmp_path / "04-draft.md").write_text("---\n" + yaml.safe_dump(meta) +
        "---\n> **Quick answer:** This is an isolated test fixture.\n\n## Evidence\nNo medical claim.\n")
    for name, value in {
        "00-authority.json": {"topic_id": "date-fixture", "operation": "new_article"},
        "01-evidence-ledger.json": {"sources": []},
        "02-serp-brief.json": {},
        "05-claim-source-map.json": {"claims": []},
    }.items():
        (tmp_path / name).write_text(json.dumps(value))
    return tmp_path


def test_delayed_draft_binds_catalog_and_jsonld_without_rewriting_evidence(tmp_path):
    compiler = module("compile_review_packet_article")
    builder = module("build_opus_article_release")
    article = compiler.compile_packet(packet(tmp_path))
    assert article["date_published"] is None
    assert article["updated_at"] == "2026-09-05"
    original = (article["prices_checked_date"], article["source_checked_through"],
                article["evidence_review"]["reviewed_at"])
    other = {"canonical_path": "/guides/other/", "date_published": "2026-07-01"}
    before = json.dumps(other, sort_keys=True)
    builder.bind_article_publication_date(article, "2026-09-27")
    schema = render(article)
    assert article["date_published"] == schema["datePublished"] == "2026-09-27"
    assert schema["dateModified"] == "2026-09-27"
    assert (article["prices_checked_date"], article["source_checked_through"],
            article["evidence_review"]["reviewed_at"]) == original
    assert json.dumps(other, sort_keys=True) == before


def test_existing_publication_keeps_first_day_on_refresh():
    builder = module("build_opus_article_release")
    predecessor = {"updated_at": "2026-08-10", "date_published": "2026-07-01",
                   "date_modified": "2026-08-10", "source_checked_through": "2026-08-09"}
    article = {"canonical_path": "/guides/date-binding-fixture/", "article_id": "fixture",
               "title": "Fixture", "h1": "Fixture", "date_published": None,
               "updated_at": "2026-09-05", "source_checked_through": "2026-09-05"}
    builder.bind_article_publication_date(article, "2026-09-27", predecessor)
    schema = render(article)
    assert article["date_published"] == schema["datePublished"] == "2026-07-01"
    assert article["date_modified"] == schema["dateModified"] == "2026-09-27"
    assert article["source_checked_through"] == "2026-09-05"
    assert predecessor == {"updated_at": "2026-08-10", "date_published": "2026-07-01",
                           "date_modified": "2026-08-10", "source_checked_through": "2026-08-09"}
    # Legacy existing rows without the new field remain renderable, without
    # retroactively inventing a different first-publication date.
    legacy = dict(article, date_published=None, updated_at="2026-08-10")
    assert render(legacy)["datePublished"] == "2026-08-10"


def test_cross_day_registered_publisher_refuses_before_network_deploy(tmp_path, monkeypatch):
    publisher = module("publish_opus_article_release")
    monkeypatch.setattr(publisher, "publication_day_pt", lambda: "2026-09-28")
    monkeypatch.setattr(publisher, "validated_post_deploy_receipts", lambda *_: None)
    calls = []
    monkeypatch.setattr(publisher, "load_module", lambda *_: calls.append("deploy") or SimpleNamespace(main=lambda: calls.append("network")))
    job = {"publication_date": "2026-09-27", "manifest_id": "fixture"}
    with pytest.raises(RuntimeError, match="publication_date_pt_day_mismatch"):
        publisher.run_deploy(job, tmp_path, {})
    assert calls == []
    monkeypatch.setattr(publisher, "reconcile_partial_deployment", lambda *_: None)
    monkeypatch.setattr(publisher, "assert_controls", lambda *_: calls.append("controls"))
    monkeypatch.setattr(publisher.accepted_failure_contract, "verify_classification", lambda *_: {})
    manifest = {"release_root": str(tmp_path)}
    manifest["manifest_payload_sha256"] = publisher.payload_hash(manifest, "manifest_payload_sha256")
    (tmp_path / "01-deployment-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="publication_date_pt_day_mismatch"):
        publisher.publish_job(dict(job, schema="glp-opus-article-release-job-v1", report_dir=str(tmp_path)))
    assert calls == []
    # On the matching hypothetical day, the same registered deploy boundary
    # reaches only a mocked engine. No network, publication or current-day pin.
    monkeypatch.setattr(publisher, "publication_day_pt", lambda: "2026-09-27")
    publisher.run_deploy(job, tmp_path, {"release_root": str(tmp_path), "cloudflare": {"expected_predecessor": "fixture"},
                                         "release_tree_sha256": "fixture", "public_tree_sha256": "fixture"})
    assert calls == ["deploy", "network"]
