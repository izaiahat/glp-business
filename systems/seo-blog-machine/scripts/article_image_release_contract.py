"""Candidate-only mixed-provider image lineage. No provider or publish calls."""
import hashlib
import json
import re
import shutil
from pathlib import Path

# The native article-image lane is GPT Image 2.5 through the Codex subscriptions
# (operator 2026-09-17, receipt
# ops/linear/approvals/OPERATOR-DECISION-20260917-gpt-image-2-5-via-codex-primary.json).
# Codex reports that builtin's completed item model id as "gpt-image-2", so that
# is the one truthful string the generator, this contract and the visual-QA test
# all bind to.
NATIVE_MODEL = "gpt-image-2"


def patch_once(text, old, new):
    if new in text:
        return text
    if text.count(old) != 1:
        raise RuntimeError('image_contract_anchor_invalid:' + old[:100])
    return text.replace(old, new, 1)


def patch_shape(text, pattern, new, applied, optional=False):
    """Anchor a provider/model assertion by its SHAPE, not the right-hand side it
    happens to carry today.

    Literal anchors vetoed six consecutive daily publications (2026-09-11..16):
    the visual-QA file gained the native provider on its `receipt.model` line, the
    exact-string anchor stopped matching, and `patch_once` raised
    `image_contract_anchor_invalid` from inside the stock-hero fallback - so
    decoration blocked publication, which it must never do. `applied` makes the
    rewrite idempotent; `optional` means "there is no such assertion to widen".
    """
    if applied in text:
        return text
    matches = list(re.finditer(pattern, text))
    if not matches and optional:
        return text
    if len(matches) != 1:
        raise RuntimeError('image_contract_anchor_invalid:' + pattern[:100])
    return text[:matches[0].start()] + new + text[matches[0].end():]


def configure(target):
    """Preserve full coverage and pixels; validate each new lane's true origin."""
    path = target / 'scripts/generate-article-imagery.py'
    text = path.read_text()
    anchor = '            if image.width < 2400 or image.height < 1200:\n'
    replacement = '''            provider = record.get("provider", "higgsfield")
            if record.get("generation_lane") == "article_release_pipeline":
                validate_release_record(record, route, path, image)
            elif provider != "higgsfield":
                raise RuntimeError(f"article_route_master_provider_invalid:{route}")
            if record.get("generation_lane") != "article_release_pipeline" and (image.width < 2400 or image.height < 1200):
'''
    text = patch_once(text, anchor, replacement)
    helper = '''
def validate_release_record(record, route, master_path, image):
    provider = record.get("provider", "higgsfield")
    spec = record.get("release_pipeline_receipt") or {}
    relative = Path(spec.get("path", ""))
    expected = Path("artifacts/ultracode/article-release-image-receipts") / f"{stem_for(route)}.json"
    receipt_path = ROOT / relative
    if relative != expected or receipt_path.is_symlink() or not receipt_path.is_file() or sha_file(receipt_path) != spec.get("sha256"):
        raise RuntimeError(f"article_release_receipt_unbound:{route}")
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("schema") != "glp-article-hero-image-v1" or receipt.get("route") != route or receipt.get("h1") != record.get("h1") or receipt.get("visual_key") != stem_for(route):
        raise RuntimeError(f"article_release_receipt_identity:{route}")
    if receipt.get("provider", "higgsfield") != provider:
        raise RuntimeError(f"article_release_receipt_provider:{route}")
    if provider == "stock":
        if receipt.get("status") != "FALLBACK" or not receipt.get("reason") or receipt.get("fallback_key") != "editorial-evidence-hero":
            raise RuntimeError(f"article_stock_fallback_receipt_invalid:{route}")
        if image.size != (1600, 900):
            raise RuntimeError(f"article_stock_master_dimensions:{route}")
        source = ROOT / "assets/derivatives/editorial-evidence-hero-1600.webp"
        if sha_file(source) != record.get("source_sha256") or sha_file(master_path) != sha_file(source):
            raise RuntimeError(f"article_stock_source_unbound:{route}")
    else:
        models = {"gpt_image_2_native": "@NATIVE_MODEL@", "higgsfield": "gpt_image_2"}
        valid = (max(image.size) >= 1536 and 1.7 <= image.width / image.height <= 1.8) if provider == "gpt_image_2_native" else (image.width >= 3800 and image.height >= 2100)
        if provider not in models or not valid or receipt.get("status") != "GENERATED" or receipt.get("model") != models[provider]:
            raise RuntimeError(f"article_release_provider_dimensions:{route}")
        source = receipt.get("selected_candidate", {}).get("source", {})
        if source.get("sha256") != record.get("source_sha256") or (source.get("width"), source.get("height")) != image.size:
            raise RuntimeError(f"article_release_source_binding:{route}")
    if receipt.get("prompt_sha256") != record.get("prompt_sha256"):
        raise RuntimeError(f"article_release_prompt_binding:{route}")
    files = receipt.get("files", [])
    expected = {f"{stem_for(route)}{suffix}": size for suffix, size in {
        "-800.avif": (800,450), "-1600.avif": (1600,900), "-800.webp": (800,450),
        "-1600.webp": (1600,900), "-800.jpg": (800,450), "-1600.jpg": (1600,900), "-og.jpg": (1200,630)}.items()}
    if len(files) != 7 or {f.get("name") for f in files} != set(expected):
        raise RuntimeError(f"article_release_family_coverage:{route}")
    for output in files:
        name = output["name"]
        file = DERIVATIVES / name
        if sha_file(file) != output.get("sha256") or file.stat().st_size != output.get("bytes") or (output.get("width"), output.get("height")) != expected[name]:
            raise RuntimeError(f"article_release_family_binding:{route}")
        if provider == "stock" and sha_file(file) != sha_file(DERIVATIVES / name.replace(stem_for(route), "editorial-evidence-hero", 1)):
            raise RuntimeError(f"article_stock_family_binding:{route}")

'''
    # One source for the native model id: the helper injected into the generator
    # must move with NATIVE_MODEL, never carry a second copy of the literal.
    helper = helper.replace('@NATIVE_MODEL@', NATIVE_MODEL)
    text = patch_once(text, 'def load_route_masters(', helper + 'def load_route_masters(')
    text = patch_once(text,
        'manifest.get("external_generation_services_used") != len(route_masters)',
        'manifest.get("external_generation_services_used") != sum(r.get("provider") != "stock" for r in route_masters.values())')
    text = patch_once(text,
        'if record.get("disposition") != "generated_from_gpt_image_2_route_master":',
        'if record.get("disposition") != ("stock_fallback_receipted" if master.get("provider") == "stock" else "generated_from_gpt_image_2_route_master"):')
    text = patch_once(text,
        'if record.get("generation_model") != "gpt_image_2":',
        'if record.get("generation_model") != ("stock" if master.get("provider") == "stock" else "gpt_image_2"):')
    path.write_text(text)
    qa = target / 'tests/article-imagery-pipeline.test.mjs'
    text = qa.read_text()
    # The provider allowlist, receipt.status and receipt.model assertions carry a
    # right-hand side that changes whenever a provider lands in the QA file, so
    # they are anchored by shape rather than by exact text.
    provider_line = 'assert.equal(receipt.provider || "higgsfield", generated.provider || "higgsfield", generated.route);'
    model_line = ('assert.equal(receipt.model, generated.provider === "stock" ? "stock" : '
                  'generated.provider === "gpt_image_2_native" ? "%s" : "gpt_image_2", generated.route);' % NATIVE_MODEL)
    text = patch_shape(
        text,
        r'assert\.ok\(\[[^\]\n]*\]\.includes\(generated\.provider \|\| "higgsfield"\), generated\.route\);',
        'assert.ok(["higgsfield", "gpt_image_2_native", "stock"].includes(generated.provider || "higgsfield"), generated.route);',
        '"stock"].includes(generated.provider',
        optional=True)
    text = patch_shape(
        text,
        r'assert\.equal\(receipt\.status, [^\n]*?, generated\.route\);',
        'assert.equal(receipt.status, generated.provider === "stock" ? "FALLBACK" : "GENERATED", generated.route);',
        'assert.equal(receipt.status, generated.provider === "stock" ? "FALLBACK"')
    text = patch_shape(
        text,
        r'assert\.equal\(receipt\.model, [^\n]*?, generated\.route\);',
        model_line,
        # The idempotency marker MUST carry the model id. A marker that stopped at
        # the "stock" branch would return early on an already-patched target and
        # silently keep a STALE model id in the QA assertion while every other
        # consumer moved on - re-opening the exact veto class this repair closed.
        model_line)
    if provider_line not in text:
        text = patch_once(text, model_line, model_line + '\n    ' + provider_line)
    replacements = [
        ('assert.equal(manifest.external_generation_services_used, manifest.route_master_count);', 'assert.equal(manifest.external_generation_services_used, Object.values(routeState.records).filter(r => r.provider !== "stock").length);'),
        ('assert.equal(receipt.selected_candidate.source.sha256, generated.source_sha256, generated.route);', 'assert.equal(generated.provider === "stock" ? receipt.stock_source_sha256 : receipt.selected_candidate.source.sha256, generated.source_sha256, generated.route);'),
        ('assert.equal(record.disposition, "generated_from_gpt_image_2_route_master");', 'assert.equal(record.disposition, routeState.records[record.route].provider === "stock" ? "stock_fallback_receipted" : "generated_from_gpt_image_2_route_master");'),
        ('assert.equal(record.generation_model, "gpt_image_2");', 'assert.equal(record.generation_model, routeState.records[record.route].provider === "stock" ? "stock" : "gpt_image_2");'),
        ('masterHashes.add(record.master_sha256);', 'if (generated.provider !== "stock") masterHashes.add(record.master_sha256);'),
        ('outputHashes.add(output.sha256);', 'if (generated.provider !== "stock") outputHashes.add(output.sha256);'),
        ('assert.equal(masterHashes.size, routes.length,', 'assert.equal(masterHashes.size, Object.values(routeState.records).filter(r => r.provider !== "stock").length,'),
        ('assert.equal(outputHashes.size, manifest.output_count,', 'assert.equal(outputHashes.size, manifest.records.filter(r => routeState.records[r.route].provider !== "stock").length * 7,'),
    ]
    for old, new in replacements:
        text = patch_once(text, old, new)
    qa.write_text(text)
    return {'status': 'PASS_MIXED_PROVIDER_CONTRACT', 'generator_sha256': sha(path), 'qa_sha256': sha(qa)}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_stock(builder, target, article, receipt, visual_key, catalogs):
    """Index exact inherited stock bytes, never invent a generation or unique art."""
    from PIL import Image
    manifest_path = target / 'artifacts/ultracode/article-image-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema') != 'glp-article-image-system-v3':
        return None
    route = article['canonical_path']
    routes = [r['canonical_path'] for c in catalogs.values() for r in c['articles']]
    inherited = set(routes) - {route}
    if len(routes) != len(set(routes)) or routes.count(route) != 1 or visual_key != 'guide-' + route.strip('/').split('/')[-1]:
        raise RuntimeError('article_stock_route_identity_invalid')
    if not receipt or receipt.get('status') != 'FALLBACK' or receipt.get('provider') != 'stock' or not receipt.get('reason'):
        raise RuntimeError('article_stock_failure_receipt_required')
    records = manifest['records']
    if {r['route'] for r in records} != inherited or len(records) != len(inherited) or manifest.get('route_master_count') != len(inherited):
        raise RuntimeError('article_stock_predecessor_coverage_invalid')
    predecessor = builder.resolve_predecessor_route_master_state(target, manifest, inherited)
    state = predecessor['state']
    builder.configure_article_route_master_state_overlay(target)
    builder.patch_article_image_visual_qa_for_release(target)
    configure(target)
    family = []
    derivative = target / 'assets/derivatives'
    for suffix, dimensions in zip(builder.IMAGE_SUFFIXES, [(800,450),(1600,900),(800,450),(1600,900),(800,450),(1600,900),(1200,630)]):
        source = derivative / ('editorial-evidence-hero' + suffix)
        dest = derivative / (visual_key + suffix)
        if source.is_symlink() or not source.is_file() or dest.exists():
            raise RuntimeError('article_stock_family_invalid')
        with Image.open(source) as im:
            im.load()
            if im.size != dimensions or im.getexif():
                raise RuntimeError('article_stock_family_dimensions_invalid')
        shutil.copy2(source, dest)
        family.append({'name': dest.name, 'width': dimensions[0], 'height': dimensions[1], 'sha256': sha(dest), 'bytes': dest.stat().st_size})
    master_rel = Path('assets/source/article-masters-pass2') / (visual_key + '.webp')
    master = target / master_rel
    if master.exists():
        raise RuntimeError('article_stock_master_collision')
    builder._make_candidate_directory_writable(master.parent)
    source = derivative / 'editorial-evidence-hero-1600.webp'
    shutil.copy2(source, master)
    source_sha = sha(source)
    prompt_sha = hashlib.sha256(('stock-fallback\0' + route + '\0' + receipt['reason']).encode()).hexdigest()
    receipt_rel = Path('artifacts/ultracode/article-release-image-receipts') / (visual_key + '.json')
    receipt_path = target / receipt_rel
    builder._make_candidate_directory_writable(receipt_path.parent)
    if receipt_path.exists():
        raise RuntimeError('article_stock_receipt_collision')
    receipt = {**receipt, 'schema': 'glp-article-hero-image-v1', 'route': route, 'h1': article['h1'], 'visual_key': visual_key, 'provider': 'stock', 'model': 'stock', 'fallback_key': 'editorial-evidence-hero', 'prompt_sha256': prompt_sha, 'stock_source_sha256': source_sha, 'files': family}
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True)+'\n')
    subject = 'Inherited editorial evidence stock hero; native generation unavailable'
    state['records'][route] = {'status': 'PASS', 'route': route, 'h1': article['h1'], 'stem': visual_key, 'provider': 'stock', 'generation_lane': 'article_release_pipeline', 'output_path': str(master_rel), 'output_sha256': source_sha, 'source_sha256': source_sha, 'prompt_sha256': prompt_sha, 'subject': subject, 'width': 1600, 'height': 900, 'release_pipeline_receipt': {'path': str(receipt_rel), 'sha256': sha(receipt_path)}}
    state_path = target / builder.ROUTE_MASTER_STATE_RELATIVE
    builder._make_candidate_directory_writable(state_path.parent)
    state_path.write_text(json.dumps(state,indent=2,sort_keys=True)+'\n')
    seed = hashlib.sha256(route.encode()).hexdigest()
    records.append({'route': route, 'stem': visual_key, 'title_sha256': hashlib.sha256(article['h1'].encode()).hexdigest(), 'seed_sha256': seed, 'seed': int(seed[:16],16), 'art_direction': 'Receipted inherited stock fallback; no burned-in text added; no new generation claimed', 'generation_model': 'stock', 'disposition': 'stock_fallback_receipted', 'master_path': str(master_rel), 'master_sha256': source_sha, 'prompt_sha256': prompt_sha, 'subject': subject, 'outputs': family})
    manifest.update(article_count=len(routes), route_master_count=len(routes), output_count=sum(len(r['outputs']) for r in records), external_generation_services_used=sum(r.get('provider') != 'stock' for r in state['records'].values()), route_master_state={'path': str(builder.ROUTE_MASTER_STATE_RELATIVE), 'sha256': sha(state_path)})
    manifest['source_files'] = [{'path': rel, 'sha256': sha(target/rel)} for rel in ['data/blog/content-batch.json','data/blog/content-national-wow-additions.json']]
    manifest_path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    return {'status': 'PASS_ARTICLE_STOCK_ROUTE_STAGED', 'provider': 'stock', 'route': route, 'visual_key': visual_key, 'master': {'path': str(master_rel), 'sha256': source_sha}, 'family': family, 'receipt': {'path': str(receipt_rel), 'sha256': sha(receipt_path)}}
