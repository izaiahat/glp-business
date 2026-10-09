"""Explicit on-page guide variants do not turn privacy holds into waivers."""
import ast
import copy
import json
import subprocess

import pytest
import yaml
from test_publication_date_binding import SCRIPTS, module, packet


def validator():
    tree = ast.parse((SCRIPTS / "validate_opus_article_packet.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "valid_capture_contract")
    scope = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "validator", "exec"), scope)
    return scope["valid_capture_contract"]


def test_on_page_guide_compilation_validation_and_template_have_no_capture(tmp_path):
    compiler = module("compile_review_packet_article")
    builder = module("build_opus_article_release")
    draft = packet(tmp_path) / "04-draft.md"
    _, front, body = draft.read_text().split("---", 2)
    meta = yaml.safe_load(front)
    meta["captureContract"] = {"mode": "on-page-only", "emailField": None,
                               "healthDataFields": [], "followUpConsent": False}
    draft.write_text("---\n" + yaml.safe_dump(meta) + "---" + body)
    article = compiler.compile_packet(tmp_path)
    assert article["capture_surface"] == {"status": "ON_PAGE_ONLY", "email_field": None,
                                           "health_data_fields": [], "follow_up_consent": False}
    assert article["surface_taxonomy"] == "medical" and article["suppress_cta"] is True
    assert validator()(article)
    wrong = copy.deepcopy(article)
    wrong["capture_surface"]["action"] = "/api/content-upgrade"
    assert not validator()(wrong)
    wrong = copy.deepcopy(article)
    wrong["sections"][0]["paragraphs"] = ["Email me a link to this guide."]
    assert not validator()(wrong)
    # Exercise the produced JS finalizer: no optional capture is added, and a
    # contradictory pre-existing form fails closed, never silently stripped.
    original = 'export function addP1ContentUpgradeSurface(html, relative = "") {\n  return html + "<form action=\\\"/api/content-upgrade\\\"></form>";\n}\n'
    patched = builder.patch_on_page_capture_finalizer(original)
    script = patched.replace("export function", "function") + '\nconsole.log(addP1ContentUpgradeSurface(process.argv[1]));'
    html = '<html data-article-capture="none"><main>On-page reading checklist</main></html>'
    rendered = subprocess.check_output(["node", "-e", script, html], text=True).strip()
    assert rendered == html
    forbidden = html.replace("</main>", '<input type="email" name="email"></main>')
    result = subprocess.run(["node", "-e", script, forbidden], capture_output=True, text=True)
    assert result.returncode and "on_page_capture_contract_conflict" in result.stderr
    assert builder.patch_on_page_capture_finalizer(patched) == patched
    draft.write_text("---\n" + yaml.safe_dump(meta) + "---" + body + "\nEmail me the guide.\n")
    with pytest.raises(RuntimeError, match="on_page_capture_promise_forbidden"):
        compiler.compile_packet(tmp_path)
