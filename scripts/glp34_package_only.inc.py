# Maintained package-only extension, embedded by generate_glp34_deploy_carrier.py.
# Package-only remains GET-only. Current preparation and approved consumption
# are separate branches below; neither changes historical authority/pins.

class PackageNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fail("package_provider_redirect_refused")


def package_get(path: str, token: str) -> dict[str, Any]:
    """Fixed-origin GET-only transport; never reuse the live mutation client."""
    prefix = f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}"
    if path != prefix and not re.fullmatch(re.escape(prefix) + r"/deployments/" + DEPLOYMENT_ID.pattern[1:-1], path):
        fail("package_read_path_invalid")
    request = urllib.request.Request(API_BASE + path, method="GET", headers={
        "Authorization": f"Bearer {token}", "User-Agent": "glp34-package-only/1"})
    try:
        with urllib.request.build_opener(PackageNoRedirect()).open(request, timeout=45) as response:
            envelope = json.loads(response.read())
    except (urllib.error.URLError, ValueError) as exc:
        fail(f"package_provider_read_failed:{type(exc).__name__}")
    if not isinstance(envelope, dict) or envelope.get("success") is not True or not isinstance(envelope.get("result"), dict):
        fail("package_provider_response_invalid")
    return envelope["result"]


def package_rollback(rollback_id: str, token: str, *, detail: bool) -> None:
    prefix = f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}"
    canonical_deployment = production_deployment(package_get(prefix, token))
    if canonical_deployment.get("id") != rollback_id or not successful_production(canonical_deployment):
        fail("package_rollback_not_current_successful_production")
    if detail:
        deployment = package_get(prefix + "/deployments/" + rollback_id, token)
        if deployment.get("id") != rollback_id or not successful_production(deployment):
            fail("package_rollback_detail_mismatch")


def package_plain_path(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        fail("package_path_must_be_absolute")
    for component in reversed([path, *path.parents]):
        if component.is_symlink():
            fail(f"package_symlink_path:{component}")
    return path


def package_tree(root: Path) -> tuple[str, list[dict[str, str]]]:
    """Site ci-release-gate.mjs treeFiles v1: recursive JS-sort + JSON rows.

    This is deliberately NOT the historical carrier's size-bearing file_closure.
    Nonregular members refuse rather than silently falling out of the census.
    """
    package_plain_path(root)
    _walk_plain(root)
    rows = []

    def walk(directory):
        for path in sorted(directory.iterdir(), key=lambda p: p.name.encode("utf-16-be")):
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                walk(path)
            elif stat.S_ISREG(mode):
                rows.append({"file": path.relative_to(root).as_posix(), "sha256": sha_file(path)})
            else:
                fail(f"package_tree_special:{path}")
    walk(root)
    if not rows:
        fail("package_tree_empty")
    # Preserve key insertion order to match JSON.stringify, not canonical().
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha_bytes(raw), rows


def f06_receipt_evidence(receipt: dict[str, Any], rows: list[dict[str, str]]) -> None:
    """Authenticate distributed evidence without relabeling it an old D1 gate.

    The sole full suite deliberately precedes the main integration. Its scope
    remains explicit; final independent acceptance binds this record and the
    prepared bytes through current_approval, not an invented integrated-suite PASS.
    """
    if (receipt.get("scope") != "f06-distributed-evidence-with-reviewed-integration"
            or receipt.get("suite_covers_integrated_head") is not False
            or receipt.get("release_authorized") is not False):
        fail("f06_evidence_scope_invalid")
    evidence = receipt.get("evidence", {})
    if set(evidence) != {"build", "full_suite", "full_tap", "source_review", "hosted_ci"}:
        fail("f06_evidence_inventory_invalid")
    values = {}
    for role, binding in evidence.items():
        path = package_plain_path(Path(binding["path"]))
        require_regular(path)
        raw = path.read_bytes()
        if sha_bytes(raw) != binding.get("sha256"):
            fail("f06_evidence_pin_mismatch:" + role)
        values[role] = raw
    build = json.loads(values["build"])
    suite = json.loads(values["full_suite"])
    ci = json.loads(values["hosted_ci"])
    if (build.get("head") != receipt["head"] or build.get("exit_code") != 0
            or sorted(build.get("files", []), key=lambda r: r["file"]) != sorted(rows, key=lambda r: r["file"])):
        fail("f06_build_binding_invalid")
    count_rows = re.findall(r"^# (tests|suites|pass|fail|cancelled|skipped|todo) ([0-9]+)$",
                            values["full_tap"].decode(), re.M)
    if len(count_rows) != 7 or len({k for k, _ in count_rows}) != 7:
        fail("f06_suite_summary_ambiguous")
    counts = {k: int(v) for k, v in count_rows}
    if (counts != suite.get("counts") or counts.get("tests", 0) <= 0
            or counts.get("pass", 0) + counts.get("skipped", 0) != counts["tests"]
            or any(counts.get(k) != 0 for k in ("fail", "cancelled", "todo"))
            or suite.get("public_unchanged") is not True
            or suite.get("head") != receipt.get("tested_head")
            or sha_bytes(values["full_tap"]) != suite.get("raw_tap_sha256")):
        fail("f06_suite_binding_invalid")
    checks = ci.get("statusCheckRollup", [])
    if (ci.get("headRefOid") != receipt["head"] or not checks
            or any(c.get("status") != "COMPLETED" or c.get("conclusion") != "SUCCESS" for c in checks)):
        fail("f06_hosted_ci_nonpass")
    if not values["source_review"].startswith(b"**APPROVE"):
        fail("f06_source_review_nonpass")


def package_build_receipt(args: argparse.Namespace, rows: list[dict[str, str]]) -> dict[str, str]:
    """Consume the site's existing gate receipt; never mint build evidence.

    The expected digest must come from reviewed build evidence, not be refreshed
    to make a changed receipt pass. This is a pinned receipt, not a signature.
    """
    if not HEX64.fullmatch(args.expected_build_receipt_sha256):
        fail("package_build_receipt_hash_invalid")
    path = package_plain_path(Path(args.build_receipt))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            fail("package_build_receipt_not_regular")
        raw = reader.read()
    if sha_bytes(raw) != args.expected_build_receipt_sha256:
        fail("package_build_receipt_hash_mismatch")
    receipt = json.loads(raw)
    if not isinstance(receipt, dict) or receipt.get("schema") not in ("glp-d1-release-result-v1", "glp-f06-distributed-release-result-v1", "glp-affiliate-sync-release-result-v1") or receipt.get("status") != "PASS":
        fail("package_build_receipt_invalid")
    if receipt.get('schema') == 'glp-affiliate-sync-release-result-v1':
        from glp_affiliate_release import validate_build_evidence
        validate_build_evidence(receipt, rows)
    if receipt.get("schema") == "glp-f06-distributed-release-result-v1":
        f06_receipt_evidence(receipt, rows)
    if receipt.get("head") != args.site_head:
        fail("package_build_receipt_head_mismatch")
    for phase in ("output_before", "output_after"):
        output = receipt.get(phase)
        if not isinstance(output, dict) or output.get("sha256") != args.expected_tree_sha256 or output.get("files") != rows:
            fail("package_build_receipt_tree_mismatch")
    return {"path": str(path), "sha256": sha_bytes(raw), "schema": receipt["schema"]}


def package_site_commit(site_head: str) -> None:
    """The only package subprocess: fixed-repository, explicit GitHub GET."""
    if not re.fullmatch(r"[0-9a-f]{40}", site_head):
        fail("package_site_head_invalid")
    command = ["gh", "api", "--hostname", "github.com", "--method", "GET",
               "repos/izaiahat/glp-site-candidate/commits/" + site_head]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
        commit = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        # Includes missing commit, auth/network failures and unavailable gh.
        # Never print subprocess output, headers or credentials.
        fail("package_site_commit_read_failed")
    if not isinstance(commit, dict) or commit.get("sha") != site_head:
        fail("package_site_commit_mismatch")


def run_package_only(args: argparse.Namespace) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", args.site_head):
        fail("package_site_head_invalid")
    if not HEX64.fullmatch(args.expected_tree_sha256):
        fail("package_expected_tree_invalid")
    if not DEPLOYMENT_ID.fullmatch(args.rollback_deployment_id):
        fail("package_rollback_id_invalid")
    public = package_plain_path(Path(args.public_root))
    output = package_plain_path(Path(args.package_dir))
    # POSIX pathlib preserves //, but Linux resolves it to the same / root.
    # Compare normalized anchors before any remote read or output creation.
    overlap_public = Path("/" + str(public).lstrip("/"))
    overlap_output = Path("/" + str(output).lstrip("/"))
    if overlap_output == overlap_public or overlap_public in overlap_output.parents or overlap_output in overlap_public.parents:
        fail("package_output_overlaps_input")
    if output.exists() or not output.parent.is_dir():
        fail("package_output_must_be_new_with_existing_parent")
    tree_hash, rows = package_tree(public)
    if tree_hash != args.expected_tree_sha256:
        fail("package_public_tree_mismatch")
    receipt_binding = package_build_receipt(args, rows)
    package_site_commit(args.site_head)
    token = read_token()
    package_rollback(args.rollback_deployment_id, token, detail=True)
    # First local write follows receipt/head/tree + current rollback checks.
    output.mkdir(mode=0o700)
    try:
        target = output / "public"
        target.mkdir()
        for row in rows:
            source = public / row["file"]
            destination = target / row["file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            # No symlink following during copy; destination is a new private tree.
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as reader:
                if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                    fail("package_copy_not_regular")
                with destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
        if package_tree(target) != (tree_hash, rows) or package_tree(public) != (tree_hash, rows):
            fail("package_tree_changed_during_copy")
        package_rollback(args.rollback_deployment_id, token, detail=False)
        package_build_receipt(args, rows)  # refuse changed receipt before completion
        manifest = {
            "schema": "glp34-deploy-package-v1", "mode": "package-only",
            "site_head": args.site_head, "site_head_binding": "sha256-pinned-site-gate-receipt-and-github-commit-get",
            "site_repository": "izaiahat/glp-site-candidate", "github_reads": 1,
            "build_receipt": receipt_binding,
            "public_root": str(public), "public_tree_sha256": tree_hash,
            "tree_hash_algorithm": "site-treeFiles-v1-json-file-sha256-recursive-js-sort",
            "file_count": len(rows), "files": rows,
            "rollback_deployment_id": args.rollback_deployment_id,
            "rollback_environment": "production", "rollback_branch": PRODUCTION_BRANCH,
            "account_id": ACCOUNT_ID, "project": PROJECT,
            "rollback_checked_at_utc": utc(), "provider_reads": 3, "provider_mutations": 0,
            "release_authorized": False, "authority_created": False, "signature_created": False,
            "scope": "public bytes only; not Functions build or release acceptance",
        }
        # A final readback must cover all writes and the last rollback GET.
        manifest_bytes = json_bytes(manifest)
        exclusive_file(output / "PACKAGE-MANIFEST.json", manifest_bytes, mode=0o444)
        persisted_manifest = (output / "PACKAGE-MANIFEST.json").read_bytes()
        if persisted_manifest != manifest_bytes:
            fail("package_final_manifest_mismatch")
        manifest_sha256 = sha_bytes(persisted_manifest)
        if package_tree(target) != (manifest["public_tree_sha256"], manifest["files"]):
            fail("package_final_tree_mismatch")
        return {"status": "PASS_PACKAGE_ONLY", "package_dir": str(output),
                "manifest_sha256": manifest_sha256,
                "public_tree_sha256": tree_hash, "file_count": len(rows),
                "provider_reads": 3, "github_reads": 1, "provider_mutations": 0, "release_authorized": False}
    except BaseException:
        shutil.rmtree(output)
        raise


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if any(mode in argv for mode in ("--prepare-current", "--publish-current", "--promote-current-existing")):
        return current_main(argv)
    if "--package-only" not in argv:
        return legacy_main(argv)
    parser = argparse.ArgumentParser(description="Package public bytes; no release authorization or deployment")
    parser.add_argument("--package-only", action="store_true", required=True)
    for flag in ("site-head", "public-root", "expected-tree-sha256", "build-receipt",
                 "expected-build-receipt-sha256", "rollback-deployment-id", "package-dir"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_package_only(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (GateError, OSError, ValueError, TypeError, AttributeError) as exc:
        # Never print credential-bearing HTTP objects or response bodies.
        error = str(exc) if isinstance(exc, GateError) else type(exc).__name__
        print(json.dumps({"status": "BLOCK", "error": error}), file=sys.stderr)
        return 1


# Current-head successor. Historical authority bodies above stay unchanged.
CURRENT_ROLLBACK = "8291d24b-a1c6-4340-bd42-2efa9f2ba2b7"
CURRENT_REPOSITORY = "izaiahat/glp-site-candidate"
# Workflow v3 operator-approved-main-release: a fresh build from merged main is not a
# pinned D1 successor; skip only the frozen D1 route-shape/binding replay for it.
MAIN_RELEASE_FRESH = False


def main_release_fresh(prepared_dir: Path) -> bool:
    """Fresh-main preparation is identified by its glp_release_main run path, not a caller flag."""
    runs = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/registry-site-sync"
    return MAIN_RELEASE_FRESH or (prepared_dir.name == "prepared" and prepared_dir.parent.parent == runs
                                  and re.fullmatch(r"main-20\d{6}T\d{6}Z", prepared_dir.parent.name) is not None)
# Rebuilt 2026-10-09 from the unchanged package-lock.json (wrangler 4.114.0, npm ci
# --ignore-scripts --no-bin-links) after a host pruner deleted node_modules.
CURRENT_TOOLCHAIN = {"closure_sha256": "6542fa639a408d4bbf71e085c0b3f2b7db6d2e6cd3f3b1b4852a7beede853b5b",
                     "file_count": 1667, "directory_count": 263, "total_bytes": 362635774}
CURRENT_AUTHORITIES = {
    "ops/linear/approvals/OPERATOR-DECISION-20260926-release-target-root-195.md":
        "f9ff3e92029b5fd6a3582ca7e7dc64fad9e699adc2b28f0b842d62d17a7870ec",
    "ops/linear/approvals/OPERATOR-DECISION-20261002-approve-everything-ship.md":
        "0b950cd8e1344ed5f6bc334a8ef437b677996c57e336627bf1010df9167b0740",
}


# One authenticated D1 successor, not a caller-selectable rollback or schema alias.
# PR29's complete native gate/public package are retained, not rebuilt here.
CURRENT_D1 = {
    "head": "3636173150662d4249ed1b01123c16d1c582de5c",
    "git_tree": "78ff74ce175aa9a76bfa3539de331d878b9f64bc",
    "gate_sha256": "49927a128ab3abc97ef87e0e1559d3041d18a2628489e4bf3f7b51af7c5bc5ba",
    "package_sha256": "c0072d40b151e1cde9d0b0e8be03608543ef09cdb103977b694dfc59c70b041f",
    "built_source_sha256": "d29f8dc3f4fc86352bc6b47d486b1bbdd78cf6e1c5aa8860d1af18d83ab0cbd5",
    "rollback": "8bb93c91-59fe-4a3b-b050-a956479c6ef5",
    "pointer_sha256": "e090daac70dae7ca9ad5c765baf500afc1ec264c1312ec4d1c357606775bc68d",
    "terminal_sha256": "a75908842f3a2dd9cc2f0685f20311e4ab849cbbfe25ffd0f52897827b3e5d41",
    "predecessor_prepared_sha256": "1d7faf179c1d780355e88c5b042ba5619523c50c279f0e60cc4a33aa48d20837",
    # Same complete Functions bytes as that predecessor; pinned compiler router
    # shape includes all 23 ordered method/mount/module/middleware entries.
    "route_shape_sha256": "6a5c03ee94766d450b7f009964f46ca3e4457c07b3141f96ac4b85a22fe70174",
    "runtime": {
        "functions": {"tree_sha256": "73592cec67a88cb22dd8db4f40e333673d7db08d1c565e9dfaacd5e182b2a3d8", "file_count": 18, "total_bytes": 244471},
        "src": {"tree_sha256": "0046125d06dd6b1e91336f3d889fc9973342068eac4d4d7ac454af145c0062b6", "file_count": 132, "total_bytes": 2544753},
        "data": {"tree_sha256": "2a84eb8e074fb4269e696ddfd489410b4d57134be0912dd52eed2c8b9cd0c0ad", "file_count": 912, "total_bytes": 89357660},
    },
}


# Accepted calculator/SEO/PostHog cohort: terminal native PASS, not the
# retained failed fbd gate. Shares the authenticated actual predecessor above.
CURRENT_D1_SUCCESSOR = {
    "head": "c7c1236e8d77d90d5f5de0eb822c48575e7d32e2",
    "git_tree": "34a65feb20390c9a916223e6ba0a3d7ecf0e1d53",
    "gate_sha256": "6d90cf6d66681ef4f06c4a8ddaf815f1bad161c3813d45bd1428f066dedcb09e",
    "package_sha256": "9e19f6be566962167f91c5d325f6ef7a8644fd12a0135664ff938d75b4485c6d",
    "built_source_sha256": "4b5fcd5d6eda98e1fbb004a4ca29b4941ac352f0619175615471cce037230a0d",
    "rollback": "8bb93c91-59fe-4a3b-b050-a956479c6ef5",
    "runtime": {
        "functions": {"tree_sha256": "73592cec67a88cb22dd8db4f40e333673d7db08d1c565e9dfaacd5e182b2a3d8", "file_count": 18, "total_bytes": 244471},
        "src": {"tree_sha256": "7b2f7eceffb4490576a08ad7bb71c65ee7f9d41133a0c352f5a6c671277fc924", "file_count": 134, "total_bytes": 2553802},
        "data": {"tree_sha256": "377bc36368f27f68113429a9ba9553d2a9241f8867d4db119c2974ca8d77ff81", "file_count": 912, "total_bytes": 89358000},
    },
}


# Genuine corrected merged-source R8 native PASS/package; preserve both prior cohorts.
CURRENT_D1_PALLAS = {
    "head": "55ba9f1076eaa53f4c3f978baf2b68e0748675cf",
    "git_tree": "d25fca602e371546bb9303c642e47b3f3ebd7b17",
    "gate_sha256": "804a94c370e00cc18ac33705ef6f6773712570f514143d3343e360f0eb7793d9",
    "package_sha256": "d8660f48346b84572d381f5ba099cb27d44df050c5a846b0075794d9f269744f",
    "built_source_sha256": "5d3f5b030fa9d6a79a3a04406445e53dc9887f3cef05999796aa51cb01e97990",
    "rollback": "8bb93c91-59fe-4a3b-b050-a956479c6ef5",
    "runtime": {
        "functions": {"tree_sha256": "035a9e01876ad73bd3a93802283e9f7f2d01e55cb1464a25d15ae32f24060b34", "file_count": 18, "total_bytes": 238861},
        "src": {"tree_sha256": "0a6170396a7f138855ccacfe5ae1eb7a08d6edcdde0fda47095a4fcd76d345a8", "file_count": 135, "total_bytes": 2573456},
        "data": {"tree_sha256": "766e996a5b30f0e5133e2f5a6b915a2b33b0085d2ae7cd8276dc61eb113ca384", "file_count": 912, "total_bytes": 89358621},
    },
}


def current_d1_binding(head: str, gate_sha256: str, package_sha256: str) -> dict[str, Any] | None:
    keys = ("head", "gate_sha256", "package_sha256")
    supplied = (head, gate_sha256, package_sha256)
    hits = [binding for binding in (CURRENT_D1, CURRENT_D1_SUCCESSOR, CURRENT_D1_PALLAS) if binding is not None
            and any(value == binding[key] for key, value in zip(keys, supplied))]
    if not hits:
        return None
    if len(hits) != 1 or supplied != tuple(hits[0][key] for key in keys):
        fail("current_d1_successor_binding_mismatch")
    return hits[0]


def current_rollback(head: str, gate_sha256: str, package_sha256: str) -> str:
    binding = current_d1_binding(head, gate_sha256, package_sha256)
    return binding["rollback"] if binding is not None else CURRENT_ROLLBACK


def current_d1_predecessor(raw: bytes) -> None:
    """Authenticate the retained native terminal chain; never mutate a global.

    The fixed pointer and terminal pins anchor the preimage. Fresh canonical
    production identity still comes from package_rollback immediately before use.
    """
    if sha_bytes(raw) != CURRENT_D1["pointer_sha256"]:
        fail("current_d1_predecessor_pointer_drift")
    pointer = json.loads(raw)
    final = current_json(Path(pointer["final_receipt"]["path"]), pointer["final_receipt"]["sha256"])
    manifest = current_json(Path(pointer["manifest"]["path"]), pointer["manifest"]["sha256"])
    terminal = current_json(Path(pointer["final_receipt"]["path"]).parent / "CURRENT-RECEIPT.json", CURRENT_D1["terminal_sha256"])
    root = package_plain_path(Path(pointer["release_root"]).parent)
    prior = current_json(root / "PREPARED.json", CURRENT_D1["predecessor_prepared_sha256"])
    identity = {"deployment_id": CURRENT_D1["rollback"], "commit_hash": pointer["site_head"]}
    if (pointer.get("status") != "PASS" or pointer.get("deployment_id") != CURRENT_D1["rollback"]
            or final.get("status") != "PASS_PRODUCTION_TERMINAL" or final.get("deployment") != {"deployment_id": identity["deployment_id"]}
            or final.get("site_head") != pointer["site_head"] or prior.get("site_head") != pointer["site_head"]
            or terminal.get("schema") != "glp43-current-deploy-receipt-v1" or terminal.get("status") != "PASS_PRODUCTION_TERMINAL"
            or terminal.get("stages", {}).get("pages_deployment") != identity
            or terminal.get("stages", {}).get("canonical_pointer_readback") != {
                "deployment_id": identity["deployment_id"], "site_head": pointer["site_head"], "pointer_sha256": sha_bytes(raw)}
            or terminal.get("binding", {}).get("prepared_manifest_sha256") != CURRENT_D1["predecessor_prepared_sha256"]
            or terminal.get("effects") != {"pages_deployments": 1, "pages_rollbacks": 0, "pointer_atomic_cas": 1,
                                          "worker_deploys": 0, "d1_mutations": 0, "cron_mutations": 0, "email_sends": 0}
            or manifest.get("release_tree_sha256") != prior["release"]["tree_sha256"]
            or pointer.get("release_tree_sha256") != prior["release"]["tree_sha256"]
            or pointer.get("deploy_directory_sha256") != prior["deploy_directory"]["tree_sha256"]
            or file_closure(root / "release") != prior["release"]
            or file_closure(root / "deploy-public") != prior["deploy_directory"]):
        fail("current_d1_predecessor_not_terminal")
    package = current_json(root / "PACKAGE-MANIFEST.json", prior["package_manifest_sha256"])
    gate = current_json(root / "gate.json", prior["build_receipt"]["sha256"])
    current_deploy_public(root / "deploy-public", package["files"], prior["compiled"]["worker_sha256"])
    rows = [[name, sha_file(root / "release" / name)] for name, _ in prior["source"]["tracked_files"]]
    if (package.get("site_head") != pointer["site_head"] or gate.get("head") != pointer["site_head"]
            or gate.get("status") != "PASS" or rows != prior["source"]["tracked_files"]
            or sha_bytes(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()) != gate.get("built_source_sha256")
            or package_tree(root / "release/public") != (prior["public_tree_sha256"], package["files"])
            or any(gate.get(phase) != {"sha256": prior["public_tree_sha256"], "files": package["files"]}
                   for phase in ("output_before", "output_after"))):
        fail("current_d1_predecessor_source_public_drift")


def current_d1_routes(routes: list[dict[str, str]]) -> None:
    rows = [[r["route"], r["mount"], r["method"], bool(r["middlewares"]), bool(r["modules"])] for r in routes]
    if sha_bytes(json.dumps(rows, separators=(",", ":")).encode()) != CURRENT_D1["route_shape_sha256"]:
        fail("current_d1_compiled_route_closure_drift")


def current_json(path: Path, digest: str) -> dict[str, Any]:
    package_plain_path(path)
    require_regular(path)
    raw = path.read_bytes()
    if not HEX64.fullmatch(digest) or sha_bytes(raw) != digest:
        fail("current_record_pin_mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict):
        fail("current_record_not_object")
    return value


def current_git(source: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(source), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60).stdout


def current_source(source: Path, head: str, tree: str, gate: dict[str, Any]) -> dict[str, Any]:
    package_plain_path(source)
    if (not re.fullmatch(r"[0-9a-f]{40}", tree)
            or current_git(source, "rev-parse", "HEAD").decode().strip() != head
            or current_git(source, "rev-parse", "HEAD^{tree}").decode().strip() != tree
            or current_git(source, "diff", "--cached", "--name-only").strip()):
        fail("current_source_git_identity_drift")
    files = current_git(source, "ls-files", "-z").decode().rstrip("\0").split("\0")
    committed = current_git(source, "ls-tree", "-rz", "--name-only", "HEAD").decode().rstrip("\0").split("\0")
    if files != committed or len(files) != len(set(files)):
        fail("current_source_index_drift")
    rows = []
    for name in files:
        path = package_plain_path(source / name)
        require_regular(path)
        rows.append([name, sha_file(path)])
    digest = sha_bytes(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode())
    diff = current_git(source, "diff", "--no-ext-diff", "--binary")
    changed = current_git(source, "diff", "--name-only").decode().strip().splitlines()
    if (digest != gate.get("built_source_sha256") or sha_bytes(diff) != gate.get("generated_diff_sha256")
            or changed != gate.get("generated_diff_paths")):
        fail("current_source_gate_drift")
    closures = {}
    for name in ("functions", "src", "data"):
        members = [p.relative_to(source).as_posix() for p in _walk_plain(source / name)[1]]
        if sorted(members) != sorted(f for f in files if f.startswith(name + "/")):
            fail("current_untracked_runtime_source")
        closures[name] = file_closure(source / name)
    expected_functions = 18 if gate.get("schema") in ("glp-f06-distributed-release-result-v1", "glp-affiliate-sync-release-result-v1") else 17
    d1_binding = next((binding for binding in (CURRENT_D1, CURRENT_D1_SUCCESSOR, CURRENT_D1_PALLAS)
                       if binding is not None and head == binding["head"]), None)
    d1_current = gate.get("schema") == "glp-d1-release-result-v1" and d1_binding is not None
    if d1_current:
        if (gate.get("head") != head or tree != d1_binding["git_tree"]
                or digest != d1_binding["built_source_sha256"] or closures != d1_binding["runtime"]):
            fail("current_d1_runtime_closure_drift")
        expected_functions = 18
    if (closures["functions"]["file_count"] != expected_functions
            or (expected_functions == 18 and not d1_current and sha_file(source / "functions/api/postback/xentraffic.js") !=
                "04e1205c2b3461d0ca8aa7106b94aea4885bdcaf3fa4ab059d0afe040d03fa77")):
        fail("current_functions_inventory_drift")
    for row in gate.get("authority_files", []):
        if row.get("file") not in ("data/go-runtime-selection.json", "data/go-runtime-promotion-authority.json",
                                    "data/tracking-allowed-hosts.json", "artifacts/ops/build-report.json",
                                    "artifacts/ops/price-index-manifest.json", "artifacts/ops/wave4-deal-ingestion-manifest.json"):
            fail("current_gate_authority_path_invalid")
        require_regular(package_plain_path(source / row["file"]))
        if sha_file(source / row["file"]) != row["sha256"]:
            fail("current_gate_authority_drift")
    go_source = (source / "functions/go/program-manifest.js").read_text()
    go_match = re.fullmatch(r"// Generated by scripts/build-staging\.mjs\. Do not edit by hand\.\nexport const GO_RUNTIME_MANIFEST = Object\.freeze\((.*)\);\s*", go_source, re.S)
    if go_match is None:
        fail("current_generated_go_manifest_shape")
    go = json.loads(go_match[1])
    if (not HEX64.fullmatch(go["manifest_sha256"])
            or go["internal_fallback"] not in (SITE + "/providers/", SITE + "/best/online-glp-1-providers/")):
        fail("current_generated_go_manifest_identity")
    return {"git_tree": tree, "built_source_sha256": digest, "tracked_files": rows,
            "go_embedded_sha256": go["manifest_sha256"], "go_internal_fallback": go["internal_fallback"],
            "generated_diff_paths": changed, "generated_diff_sha256": sha_bytes(diff),
            "runtime": closures, "authority_files": gate["authority_files"],
            "go_manifest_sha256": sha_file(source / "functions/go/program-manifest.js"),
            "wrangler_config_sha256": sha_file(source / "wrangler.toml")}


def current_pointer(rollback: str | None = None) -> dict[str, Any]:
    rollback = CURRENT_ROLLBACK if rollback is None else rollback
    raw, st = capture_pointer(POINTER)
    parsed = json.loads(raw)
    if rollback == CURRENT_D1["rollback"]:
        current_d1_predecessor(raw)
    elif rollback != CURRENT_ROLLBACK:
        fail("current_pointer_unknown_predecessor")
    if parsed.get("deployment_id") != rollback:
        fail("current_pointer_rollback_mismatch")
    return {"path": str(POINTER), "bytes_base64": base64.b64encode(raw).decode(),
            "sha256": sha_bytes(raw), "bytes": len(raw), "st_dev": st.st_dev, "st_ino": st.st_ino,
            "uid": st.st_uid, "gid": st.st_gid, "mode": f"{_mode(st):04o}", "nlink": st.st_nlink,
            "deployment_id": parsed["deployment_id"], "release_root": parsed["release_root"]}


def current_routes(worker: bytes) -> list[dict[str, str]]:
    # Read the pinned compiler's actual router, not an invented _routes.json.
    blocks = re.findall(rb"var routes = \[\n(.*?)\n\];", worker, re.S)
    if len(blocks) != 1:
        fail("current_compiled_router_shape")
    pattern = (rb'\{\s*routePath: "([^"]+)",\s*mountPath: "([^"]*)",\s*method: "([^"]*)",'
               rb'\s*middlewares: \[([^\]]*)\],\s*modules: \[([^\]]*)\]\s*\}')
    rows = [dict(zip(("route", "mount", "method", "middlewares", "modules"),
                     (v.decode() for v in match))) for match in re.findall(pattern, blocks[0])]
    if (len(rows) != blocks[0].count(b"routePath:") or not rows
            or not any(r["route"] == "/go/:program" and r["modules"] for r in rows)
            or not any(r["route"] == "/api/postback/affiliati" and r["modules"] for r in rows)
            or not any(r["route"] == "/providers" and r["middlewares"] and not r["modules"] for r in rows)
            or b'env["ASSETS"].fetch(request)' not in worker):
        fail("current_compiled_routes_missing")
    return rows


def current_compile(workspace: Path) -> dict[str, Any]:
    pages_out = workspace / ".wrangler/pages-functions-build"
    command = [str(NODE), str(WRANGLER), "pages", "functions", "build", "functions", "--outdir", str(pages_out)]
    proc = subprocess.run(command, cwd=str(workspace), env=wrangler_env(workspace),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=1200)
    if proc.returncode or any(word in (proc.stdout + proc.stderr).lower() for word in ("download", "registry.npmjs")):
        fail("current_functions_build_failed")
    path = pages_out / "index.js"
    raw = path.read_bytes()
    # Wrangler 4.114.0 getPagesProjectRoot: nearest package.json, else cwd.
    # Its esbuild projectRoot is functions/, not that Pages temporary root.
    project_root = next((p for p in (workspace, *workspace.parents) if (p / "package.json").is_file()), workspace)
    routes_tmp = os.path.relpath(project_root / ".wrangler/tmp", workspace / "functions").encode()
    fixed, count = re.subn(rb"(?m)^// " + re.escape(routes_tmp) + rb"/pages-[^/\r\n]+/functionsRoutes-[^\r\n]+$",
                          b"// r13-canonical-functions-routes.mjs", raw)
    if count != 1:
        fail("current_wrangler_nondeterminism_shape_drift")
    atomic_file(path, fixed)
    return {"argv": command, "returncode": proc.returncode,
            "stdout_sha256": sha_bytes(proc.stdout.encode()), "stderr_sha256": sha_bytes(proc.stderr.encode()),
            "raw_worker_sha256": sha_bytes(raw), "worker_sha256": sha_bytes(fixed),
            "pages_random_comment_replacements": count, "routes": current_routes(fixed)}


def current_deploy_public(deploy: Path, public_rows: list[dict[str, str]], worker_sha256: str) -> None:
    """Require accepted public bytes plus exactly the compiled worker, not a self-recorded closure."""
    rows = package_tree(deploy)[1]
    if ([r for r in rows if r["file"] != "_worker.js"] != public_rows
            or [r for r in rows if r["file"] == "_worker.js"] !=
            [{"file": "_worker.js", "sha256": worker_sha256}]):
        fail("current_deploy_public_package_mismatch")


def run_prepare_current(args: argparse.Namespace) -> dict[str, Any]:
    source, package, output = [package_plain_path(Path(p)) for p in
                               (args.source_root, args.package_dir, args.prepared_dir)]
    for input_path in (source, package, Path(args.build_receipt)):
        a, b = Path("/" + str(input_path).lstrip("/")), Path("/" + str(output).lstrip("/"))
        if a == b or a in b.parents or b in a.parents:
            fail("current_output_overlaps_input")
    if output.exists() or not output.parent.is_dir():
        fail("current_output_must_be_new")
    manifest = current_json(package / "PACKAGE-MANIFEST.json", args.expected_package_manifest_sha256)
    rollback = current_rollback(args.site_head, args.expected_build_receipt_sha256, args.expected_package_manifest_sha256)
    if (manifest.get("schema") != "glp34-deploy-package-v1" or manifest.get("site_head") != args.site_head
            or manifest.get("site_repository") != CURRENT_REPOSITORY or manifest.get("project") != PROJECT
            or manifest.get("account_id") != ACCOUNT_ID or manifest.get("rollback_branch") != PRODUCTION_BRANCH
            or manifest.get("rollback_deployment_id") != rollback
            or manifest.get("build_receipt", {}).get("sha256") != args.expected_build_receipt_sha256):
        fail("current_package_binding_mismatch")
    args.expected_tree_sha256 = manifest["public_tree_sha256"]
    public = package / "public"
    tree_hash, rows = package_tree(public)
    if (tree_hash != args.expected_tree_sha256 or rows != manifest.get("files")
            or any(r["file"] in ("_worker.js", "_routes.json") for r in rows)):
        fail("current_public_package_drift")
    route_specs = current_route_specs(public)  # Resolve required static files before any effects.
    gate_binding = package_build_receipt(args, rows)
    gate = current_json(Path(args.build_receipt), args.expected_build_receipt_sha256)
    binding = current_source(source, args.site_head, args.git_tree, gate)
    holds = next((s["evidence"]["raw_held"] for s in gate["steps"] if s["id"] == "affiliate-census" and s["status"] == "PASS"), None)
    if (not holds or holds.get("anchors") != [] or holds.get("routes") != [] or not holds.get("held")
            or any(v != 0 for v in holds["counts"].values()) or "fella-health" not in holds["counts"]):
        fail("current_gate_holds_nonpass")
    pointer = current_pointer(rollback)
    verify_immutable_tree(TOOLCHAIN, CURRENT_TOOLCHAIN, toolchain=True)
    subprocess.run([sys.executable, str(BUSINESS / "scripts/candidate_build_lock.py"),
                    "require-held", "--seat", args.build_seat], check=True, capture_output=True, timeout=30)
    package_site_commit(args.site_head)
    token = read_token()
    package_rollback(rollback, token, detail=True)
    output.mkdir(mode=0o700)
    try:
        release, compile_root = output / "release", output / "compile"
        release.mkdir(); compile_root.mkdir()
        for name in dict.fromkeys([r[0] for r in binding["tracked_files"]] + [r["file"] for r in binding["authority_files"]]):
            destination = release / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, destination, follow_symlinks=False)
        for name in ("functions", "src", "data"):
            copytree_exact(source / name, compile_root / name)
        if current_source(source, args.site_head, args.git_tree, gate) != binding:
            fail("current_source_changed_during_copy")
        for name, digest in binding["tracked_files"] + [[r["file"], r["sha256"]] for r in binding["authority_files"]]:
            if sha_file(release / name) != digest:
                fail("current_release_copy_drift")
        compiled = current_compile(compile_root)
        if rollback == CURRENT_D1["rollback"] and not MAIN_RELEASE_FRESH:
            current_d1_routes(compiled["routes"])
        if (gate.get("schema") in ("glp-f06-distributed-release-result-v1", "glp-affiliate-sync-release-result-v1")
                and not any(r["route"] == "/api/postback/xentraffic" and r["modules"] for r in compiled["routes"])):
            fail("f06_compiled_xentraffic_route_missing")
        for name, closure in binding["runtime"].items():
            if file_closure(compile_root / name) != closure:
                fail("current_compile_input_drift")
        copytree_exact(public, release / "public")
        deploy = output / "deploy-public"
        copytree_exact(public, deploy)
        shutil.copyfile(compile_root / ".wrangler/pages-functions-build/index.js", deploy / "_worker.js")
        if sha_file(deploy / "_worker.js") != compiled["worker_sha256"]:
            fail("current_worker_copy_drift")
        shutil.rmtree(compile_root)
        exclusive_file(output / "gate.json", Path(args.build_receipt).read_bytes(), 0o444)
        exclusive_file(output / "PACKAGE-MANIFEST.json", (package / "PACKAGE-MANIFEST.json").read_bytes(), 0o444)
        prepared = {"schema": "glp43-current-prepared-v1", "prepared_dir": str(output),
                    "site_head": args.site_head, "site_repository": CURRENT_REPOSITORY,
                    "project": PROJECT, "branch": PRODUCTION_BRANCH, "account_id": ACCOUNT_ID,
                    "public_tree_sha256": tree_hash, "package_manifest_sha256": args.expected_package_manifest_sha256,
                    "build_receipt": gate_binding, "source": binding, "compiled": compiled,
                    "toolchain": CURRENT_TOOLCHAIN, "rollback_deployment_id": rollback,
                    "predecessor_pointer": pointer, "release": file_closure(release),
                    "deploy_directory": file_closure(deploy), "holds": holds, "route_specs": route_specs,
                    "provider_reads": 3, "provider_mutations": 0, "release_authorized": False}
        package_rollback(rollback, token, detail=False)
        verify_signed_predecessor(pointer)
        if current_source(source, args.site_head, args.git_tree, gate) != binding or package_tree(public) != (tree_hash, rows):
            fail("current_inputs_changed_before_completion")
        current_deploy_public(deploy, rows, compiled["worker_sha256"])
        raw = json_bytes(prepared)
        exclusive_file(output / "PREPARED.json", raw, 0o444)
        current_prepared(output, sha_bytes(raw))
        return {"status": "PASS_PREPARED_CURRENT", "prepared_dir": str(output),
                "manifest_sha256": sha_bytes(raw), "deploy_directory": prepared["deploy_directory"],
                "compiled_worker_sha256": compiled["worker_sha256"],
                "provider_reads": 3, "provider_mutations": 0, "release_authorized": False}
    except BaseException:
        shutil.rmtree(output)
        raise


def current_prepared(root: Path, digest: str) -> dict[str, Any]:
    value = current_json(root / "PREPARED.json", digest)
    rollback = current_rollback(value.get("site_head"), value.get("build_receipt", {}).get("sha256"), value.get("package_manifest_sha256"))
    if (value.get("schema") != "glp43-current-prepared-v1" or value.get("prepared_dir") != str(root)
            or value.get("site_repository") != CURRENT_REPOSITORY or value.get("project") != PROJECT
            or value.get("branch") != PRODUCTION_BRANCH or value.get("account_id") != ACCOUNT_ID
            or value.get("rollback_deployment_id") != rollback or value.get("toolchain") != CURRENT_TOOLCHAIN):
        fail("current_prepared_identity_mismatch")
    if (file_closure(root / "release") != value["release"]
            or file_closure(root / "deploy-public") != value["deploy_directory"]
            or sha_file(root / "deploy-public/_worker.js") != value["compiled"]["worker_sha256"]):
        fail("current_prepared_bytes_drift")
    package = current_json(root / "PACKAGE-MANIFEST.json", value["package_manifest_sha256"])
    current_deploy_public(root / "deploy-public", package["files"], value["compiled"]["worker_sha256"])
    gate = current_json(root / "gate.json", value["build_receipt"]["sha256"])
    if rollback == CURRENT_D1["rollback"] and not main_release_fresh(root):
        current_d1_predecessor(base64.b64decode(value["predecessor_pointer"]["bytes_base64"], validate=True))
        current_d1_routes(current_routes((root / "deploy-public/_worker.js").read_bytes()))
        binding = current_d1_binding(value["site_head"], value["build_receipt"]["sha256"], value["package_manifest_sha256"])
        if (binding is None or value["source"]["git_tree"] != binding["git_tree"]
                or value["source"]["built_source_sha256"] != binding["built_source_sha256"]
                or {name: file_closure(root / "release" / name) for name in ("functions", "src", "data")} != binding["runtime"]
                or package.get("rollback_deployment_id") != rollback):
            fail("current_d1_prepared_binding_drift")
    if (package_tree(root / "release/public") != (value["public_tree_sha256"], package["files"])
            or current_route_specs(root / "release/public") != value["route_specs"]):
        fail("current_prepared_public_drift")
    return value


def current_approval(args: argparse.Namespace, prepared: dict[str, Any]) -> dict[str, Any]:
    review = current_json(Path(args.approval), args.expected_approval_sha256)
    operator_sync = review.get('schema') == 'glp-affiliate-sync-operator-release-v1'
    if operator_sync:
        from glp_affiliate_release import validate_operator_release
        validate_operator_release(review, prepared)
    if (review.get("schema") not in ("glp42-designated-astra-release-review-v1", "glp-affiliate-sync-operator-release-v1")
            or review.get("verdict") != "APPROVE" or review.get("release_accepted") is not True
            or review.get("blocking_findings") != []):
        fail("current_release_not_approved")
    reviewer = review.get("reviewer", {})
    if not operator_sync and (reviewer.get("model") not in ("gpt-6-astra-900k", "gpt-6-astra") or reviewer.get("provider") != "openai-codex"
            or "not an author" not in reviewer.get("role", "")
            or reviewer.get("authority") != list(CURRENT_AUTHORITIES)[1]
            or reviewer.get("original_target_authority") != list(CURRENT_AUTHORITIES)[0]):
        fail("current_designated_independent_reviewer_required")
    required = {"repository": CURRENT_REPOSITORY, "head": prepared["site_head"],
                "git_tree": prepared["source"]["git_tree"], "public_tree_sha256": prepared["public_tree_sha256"],
                "package_manifest_sha256": prepared["package_manifest_sha256"],
                "project": PROJECT, "rollback_branch": PRODUCTION_BRANCH, "rollback_environment": "production",
                "rollback_deployment_id": prepared["rollback_deployment_id"],
                "prepared_manifest_sha256": args.expected_prepared_manifest_sha256,
                "deploy_directory_sha256": prepared["deploy_directory"]["tree_sha256"],
                "compiled_worker_sha256": prepared["compiled"]["worker_sha256"]}
    if any(review.get("subject", {}).get(k) != v for k, v in required.items()):
        fail("current_approval_subject_mismatch")
    if review.get("reused_technical_acceptance", {}).get("gate_sha256") != prepared["build_receipt"]["sha256"]:
        fail("current_approval_gate_mismatch")
    for name, pin in CURRENT_AUTHORITIES.items():
        require_regular(package_plain_path(BUSINESS / name))
        if sha_file(BUSINESS / name) != pin:
            fail("current_operator_authority_drift")
    if not args.authorize_production:
        fail("current_explicit_production_authorization_required")
    return review


class CurrentNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Read a Location header; never visit a provider/tracking URL.


def current_runtime_readback(deployment: dict[str, Any], prepared: dict[str, Any]) -> list[dict[str, Any]]:
    bases = [deployment["url"].rstrip("/"), f"https://{PROJECT}.pages.dev", SITE]
    paths = ["/go/_manifest", "/best/online-glp-1-providers/", "/api/postback/affiliati", "/providers/?glp43=readback"] + ["/go/" + row["id"] for row in prepared["holds"]["held"]]
    results = []
    for base in bases:
        for path in paths:
            if not re.fullmatch(r"/(?:best/online-glp-1-providers/|go/(?:_manifest|[a-z0-9-]+)|api/postback/affiliati|providers/\?glp43=readback)", path):
                fail("current_runtime_probe_path_invalid")
            method = "GET" if path.startswith("/best/") else "HEAD"
            request = urllib.request.Request(base + path, method=method, headers={"User-Agent": "glp-r13-authority-carrier/3",
                                                                             "Cache-Control": "no-cache"})
            try:
                response = urllib.request.build_opener(CurrentNoRedirect()).open(request, timeout=30)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status_code, headers = response.code, response.headers
            if path.startswith("/go/"):
                go_status = (status_code == 200 if path == "/go/_manifest" else
                             status_code == 307 and headers.get("Location") == prepared["source"]["go_internal_fallback"])
                ok = (go_status and headers.get("X-GLP-Program-Manifest-SHA256") == prepared["source"]["go_embedded_sha256"]
                      and "no-store" in headers.get("Cache-Control", "") and "noindex" in headers.get("X-Robots-Tag", ""))
            elif path.startswith("/best/"):
                ok = status_code == 301 and headers.get("Location") == base + "/providers/"
            elif path.startswith("/api/"):
                ok = status_code == 405 and "no-store" in headers.get("Cache-Control", "") and "noindex" in headers.get("X-Robots-Tag", "")
            else:
                ok = status_code == 200 and headers.get("X-Robots-Tag") == "noindex, follow"
            if not ok:
                fail("current_runtime_readback_failed:" + path)
            results.append({"base": base, "path": path, "method": method, "status": status_code,
                            "location": headers.get("Location"), "robots": headers.get("X-Robots-Tag"),
                            "go_manifest_sha256": headers.get("X-GLP-Program-Manifest-SHA256"),
                            "cache_control": headers.get("Cache-Control")})
    return results


def current_route_specs(root: Path) -> list[dict[str, Any]]:
    # Bind canonical families, disclosures, held surfaces, runtime assets and sitemap.
    names = ["index.html", "providers/index.html",
             "tools/glp-1-cost-calculator/index.html", "lp/quiz/index.html", "quiz/provider-fit/index.html",
             "reviews/nu-image-medical-review/index.html", "reviews/mydrhank/index.html",
             "privacy/index.html", "privacy/cookies/index.html", "sitemap.xml"]
    names += [r["path"] for r in file_rows(root) if r["path"].startswith("assets/") and r["path"].endswith((".js", ".css"))]
    return [{"url_path": "/" + name.removesuffix("index.html"), "sha256": sha_file(root / name),
             "bytes": (root / name).stat().st_size} for name in names]


# Exact mailto/span reversal plus the retained providers plaintext shape.
# Do not import the historical normalizer's query links or generic cleanup.
CURRENT_CF_EMAIL_ANCHOR_RE = re.compile(
    rb'<a href="/cdn-cgi/l/email-protection#([0-9A-Fa-f]+)"><span class="__cf_email__" data-cfemail="([0-9A-Fa-f]+)">\[email&#160;protected\]</span></a>'
)
CURRENT_CF_EMAIL_PLAIN_RE = re.compile(
    rb'<a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="([0-9A-Fa-f]+)">\[email&#160;protected\]</a>'
)
CURRENT_CF_EMAIL_DECODER_SCRIPT = b'<script data-cfasync="false" src="/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js"></script>'


def current_restore_cf_email(value: bytes) -> bytes:
    """Reverse only proven CF shapes; the caller still requires all source bytes."""
    def decode_cloudflare_hex(encoded: bytes, *, plaintext: bool = False) -> bytes:
        if len(encoded) < 4 or len(encoded) % 2:
            fail("cloudflare_email_hex_invalid")
        try:
            raw = bytes.fromhex(encoded.decode("ascii"))
            decoded = bytes(byte ^ raw[0] for byte in raw[1:]).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            fail("cloudflare_email_decode_invalid")
        # The mailto allowlist is unchanged. Only the exact retained plaintext
        # spelling is supported; this is not a general provider-email decoder.
        allowed = ({"care@BiltRx.com"} if plaintext else
                   {"ops@glpclinicscout.com", "support@glpclinicscout.com", "privacy@glpclinicscout.com"})
        if decoded not in allowed:
            fail("cloudflare_email_unapproved")
        return decoded.encode("ascii")

    def restore_anchor(match: re.Match[bytes]) -> bytes:
        href_address = decode_cloudflare_hex(match.group(1))
        span_address = decode_cloudflare_hex(match.group(2))
        if href_address != span_address:
            fail("cloudflare_email_anchor_span_mismatch")
        return b'<a href="mailto:' + href_address + b'">' + href_address + b'</a>'

    anchors = list(CURRENT_CF_EMAIL_ANCHOR_RE.finditer(value))
    plain = list(CURRENT_CF_EMAIL_PLAIN_RE.finditer(value))
    # Retained providers evidence has two regions flushed at their next original
    # scripts. Privacy has mailto anchors after its last original script; its
    # one decoder instead ends immediately at the actual </body></html> boundary.
    # Neither shape permits arbitrary placement, scripts, or byte changes.
    if plain and (len(plain) != 1 or len(anchors) != 1 or plain[0].end() > anchors[0].start()):
        fail("cloudflare_email_plaintext_shape_invalid")
    restored, count = CURRENT_CF_EMAIL_ANCHOR_RE.subn(restore_anchor, value)
    restored = CURRENT_CF_EMAIL_PLAIN_RE.sub(
        lambda match: decode_cloudflare_hex(match.group(1), plaintext=True), restored)
    script_count = value.count(CURRENT_CF_EMAIL_DECODER_SCRIPT)
    if count or plain:
        boundaries = {value.find(b"<script", match.end()) for match in [*anchors, *plain]}
        if (script_count < 1 or script_count != len(boundaries)
                or (plain and len(boundaries) != 2)):
            fail("cloudflare_email_decoder_script_count_invalid")
        for position in boundaries:
            after = position + len(CURRENT_CF_EMAIL_DECODER_SCRIPT)
            # The only supported terminal insertion is immediately before the
            # literal closing body, with no later original script. Exact source
            # equality after restoration remains the final authority.
            terminal_body = value[after:] == b"</body></html>"
            if (position < 0 or not value.startswith(CURRENT_CF_EMAIL_DECODER_SCRIPT, position)
                    or not (value.startswith((b"<script ", b"<script>"), after) or terminal_body)):
                fail("cloudflare_email_decoder_script_location_invalid")
        restored = restored.replace(CURRENT_CF_EMAIL_DECODER_SCRIPT, b"")
    elif script_count:
        fail("cloudflare_email_decoder_without_anchor")
    if any(marker in restored for marker in (b"/cdn-cgi/l/email-protection", b"__cf_email__",
                                             b"data-cfemail", b"email-decode.min.js")):
        fail("cloudflare_email_transform_unhandled")
    return restored


def current_route_probe(base: str, item: dict[str, Any], public: Path,
                        receipt_path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    path = item["url_path"]
    if (not isinstance(path, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]*", path)
            or any(part in (".", "..") for part in path.split("/"))):
        fail("current_route_path_invalid")
    source = package_plain_path(public / (path.lstrip("/") + ("index.html" if path.endswith("/") else "")))
    require_regular(source)
    expected = source.read_bytes()
    if len(expected) != item["bytes"] or sha_bytes(expected) != item["sha256"]:
        fail("current_route_expected_bytes_drift")
    url = base + path
    request = urllib.request.Request(url, headers={"User-Agent": "glp-r13-authority-carrier/3",
                                                   "Cache-Control": "no-cache"})
    try:
        try:
            response = urllib.request.build_opener(CurrentNoRedirect()).open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            status_code, headers = response.status, response.headers
    except urllib.error.URLError as exc:
        fail(f"route_probe_failed:{url}:{type(exc).__name__}")
    # Use the existing run report + receipt. Save RAW bytes and their digest
    # before status, parsing or comparison can refuse; never overwrite a probe.
    body_path = receipt_path.parent / ("route-probe-" + uuid.uuid4().hex + ".body")
    exclusive_file(body_path, body)
    row = {"url": url, "status": status_code, "bytes": len(body), "body_sha256": sha_bytes(body),
           "body_path": str(body_path), "expected_sha256": item["sha256"], "expected_bytes": item["bytes"],
           "headers": {name: headers.get(name) for name in
                       ("Content-Type", "Cache-Control", "ETag", "Location", "CF-Cache-Status", "Content-Encoding")}}
    receipt["stages"].setdefault("route_probe_raw", []).append(row)
    checkpoint(receipt_path, receipt)
    compared, comparison = body, "raw_exact"
    if (status_code == 200 and body != expected and base == SITE and source.suffix == ".html"
            and headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "text/html"):
        compared, comparison = current_restore_cf_email(body), "exact_cf_email_reversal"
    if (status_code != 200 or len(compared) != item["bytes"] or sha_bytes(compared) != item["sha256"]
            or compared != expected):
        fail(f"route_probe_invariant_failed:{url}:{status_code}")
    row.update(comparison=comparison, compared_sha256=sha_bytes(compared), compared_bytes=len(compared))
    checkpoint(receipt_path, receipt)
    return row


def current_route_matrix(client: CloudflareClient, deployment: dict[str, Any],
                         route_specs: list[dict[str, Any]], public: Path,
                         receipt_path: Path, receipt: dict[str, Any], *, timeout_seconds: int = 120) -> list[dict[str, Any]]:
    # Carry historical ordering, canonical-head guard and convergence policy.
    # The historical route_matrix/route_probe and their authority pins stay frozen.
    deployment_id = str(deployment.get("id", ""))
    immutable = str(deployment.get("url", "")).rstrip("/")
    if (not DEPLOYMENT_ID.fullmatch(deployment_id) or not immutable.startswith("https://")
            or not immutable.endswith(f".{PROJECT}.pages.dev")):
        fail("deployment_immutable_identity_invalid")
    bases = [immutable, f"https://{PROJECT}.pages.dev", SITE]
    deadline = time.monotonic() + timeout_seconds
    last_error = "route_matrix_not_attempted"
    while True:
        project = client.project()
        canonical_head = production_deployment(project)
        if canonical_head.get("id") != deployment_id:
            fail(f"canonical_head_changed_during_route_matrix:{canonical_head.get('id')}")
        try:
            results = []
            for base in bases:
                for item in route_specs:
                    results.append(current_route_probe(base, item, public, receipt_path, receipt))
            return results
        except GateError as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                fail(f"route_matrix_did_not_converge:{last_error}")
            time.sleep(5)


def current_pointer_intent(root: Path, prepared: dict[str, Any], deployment_id: str, run_dir: Path) -> dict[str, Any]:
    spec = prepared["predecessor_pointer"]
    predecessor = base64.b64decode(spec["bytes_base64"], validate=True)
    manifest_path, terminal_path = run_dir / "01-deployment-manifest.json", run_dir / "10-final-publication-receipt.json"
    manifest = json_bytes({"schema": "glp-opus-release-deployment-manifest-v1", "release_root": str(root / "release"),
                           "release_rows": file_rows(root / "release"), "release_tree_sha256": prepared["release"]["tree_sha256"],
                           "site_head": prepared["site_head"], "deploy_directory": prepared["deploy_directory"]})
    terminal = json_bytes({"schema": "glp-opus-production-terminal-receipt-v1", "status": "PASS_PRODUCTION_TERMINAL",
                           "deployment": {"deployment_id": deployment_id}, "site_head": prepared["site_head"],
                           "evidence_sha256": {manifest_path.name: sha_bytes(manifest)},
                           "immutable_closure": {"release_tree_sha256": prepared["release"]["tree_sha256"],
                                                  "public_tree_sha256": file_closure(root / "release/public")["tree_sha256"]}})
    successor = json.loads(predecessor)
    successor.update({"release_root": str(root / "release"), "deployment_id": deployment_id, "status": "PASS",
                      "manifest": {"path": str(manifest_path), "sha256": sha_bytes(manifest)},
                      "final_receipt": {"path": str(terminal_path), "sha256": sha_bytes(terminal)},
                      "release_tree_sha256": prepared["release"]["tree_sha256"],
                      "public_tree_sha256": file_closure(root / "release/public")["tree_sha256"],
                      "site_head": prepared["site_head"], "deploy_directory_sha256": prepared["deploy_directory"]["tree_sha256"],
                      "supersedes": {"deployment_id": prepared["rollback_deployment_id"], "release_root": spec["release_root"]}})
    for key in ("deployment_reconciliation", "gsc_closeout"):
        successor.pop(key, None)
    raw = json_bytes(successor)
    return {"pointer_path": str(POINTER), "lock_path": str(LOCK_PATH),
            "predecessor_bytes_base64": spec["bytes_base64"], "successor_bytes_base64": base64.b64encode(raw).decode(),
            "predecessor_stat": {k: spec[k] for k in ("st_dev", "st_ino", "uid", "gid", "mode", "nlink")},
            "final_mode": spec["mode"], "final_uid": spec["uid"], "final_gid": spec["gid"],
            "temp_name": "." + POINTER.name + ".glp43-" + prepared["site_head"] + ".tmp",
            "artifacts": [{"path": str(path), "mode": "0600", "bytes_base64": base64.b64encode(data).decode()}
                          for path, data in ((manifest_path, manifest), (terminal_path, terminal))]}


def current_deployments(client: CloudflareClient) -> list[dict[str, Any]]:
    """Current reconciliation: server-default page size, all environments.

    Keep the historical reader frozen; retain its full-pagination guards here.
    The provider accepts omitted per_page but rejected the legacy explicit 100.
    """
    deployments = []
    seen = set()
    page = 1
    totals = None
    while True:
        response = client.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments?page={page}", envelope=True)
        result = response.get("result")
        info = response.get("result_info")
        if not isinstance(result, list) or not isinstance(info, dict):
            fail("deployment_list_shape_invalid")
        pages, count = info.get("total_pages"), info.get("total_count")
        if (type(pages) is not int or type(count) is not int or pages < 0 or count < 0
                or type(info.get("page")) is not int or info["page"] != page):
            fail("deployment_list_pagination_invalid")
        if totals is None:
            totals = (pages, count)
        if totals != (pages, count) or (pages == 0 and (page != 1 or count != 0 or result)):
            fail("deployment_list_changed_during_read")
        for item in result:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not DEPLOYMENT_ID.fullmatch(item["id"]) or item["id"] in seen
                    or item.get("environment") not in ("production", "preview")):
                fail("deployment_list_identity_invalid")
            seen.add(item["id"])
            deployments.append(item)
        if page >= pages:
            if len(deployments) != count:
                fail("deployment_list_incomplete")
            return deployments
        page += 1


def run_publish_current(args: argparse.Namespace) -> dict[str, Any]:
    root = package_plain_path(Path(args.prepared_dir))
    prepared = current_prepared(root, args.expected_prepared_manifest_sha256)
    rollback = prepared["rollback_deployment_id"]
    current_approval(args, prepared)  # No lock, client, credential, or output before approval.
    verify_immutable_tree(TOOLCHAIN, CURRENT_TOOLCHAIN, toolchain=True)
    lock_fd = acquire_lock()
    try:
        run_dir = SEO_REPORT_ROOT / ("current-release-" + prepared["site_head"])
        receipt_path = run_dir / "CURRENT-RECEIPT.json"
        binding = {"prepared_manifest_sha256": args.expected_prepared_manifest_sha256,
                   "approval_sha256": args.expected_approval_sha256}
        if receipt_path.exists():
            receipt = load_json(receipt_path)
            if receipt.get("binding") != binding or receipt.get("status") != "ACTIVE":
                fail("current_release_consumed_or_binding_changed")
        else:
            verify_signed_predecessor(prepared["predecessor_pointer"])
            receipt = {"schema": "glp43-current-deploy-receipt-v1", "status": "ACTIVE", "binding": binding,
                       "site_head": prepared["site_head"], "rollback_deployment_id": rollback,
                       "deploy_directory": prepared["deploy_directory"], "stages": {}}
            exclusive_file(receipt_path, json_bytes(receipt))
        stages = receipt["stages"]
        client = CloudflareClient(read_token())
        head = prepared["site_head"]
        current = production_deployment(client.project())
        candidates = [d for d in current_deployments(client) if deployment_commit(d) == head
                      and d.get("environment") == "production" and deployment_branch(d) == PRODUCTION_BRANCH]
        if "rollback_intent" in stages:
            # An uncertain rollback is also reconciliation-only: never replay it.
            if current.get("id") == rollback and successful_production(current):
                verify_signed_predecessor(prepared["predecessor_pointer"])
                receipt["status"] = "BLOCKED_ROLLED_BACK"
                checkpoint(receipt_path, receipt)
            fail("current_prior_rollback_no_replay")
        if "pages_deploy_intent" not in stages:
            if current.get("id") != rollback or not successful_production(current) or candidates:
                fail("current_predecessor_or_existing_candidate_mismatch")
            current_prepared(root, args.expected_prepared_manifest_sha256)
            current_approval(args, prepared)
            verify_signed_predecessor(prepared["predecessor_pointer"])
            stages["pages_deploy_intent"] = {"predecessor_id": rollback, "commit_hash": head, "created_at_utc": utc()}
            checkpoint(receipt_path, receipt)
            immediate = production_deployment(client.project())
            if immediate.get("id") != rollback or not successful_production(immediate):
                fail("current_canonical_changed_before_publish")
            current_prepared(root, args.expected_prepared_manifest_sha256)
            cwd = run_dir / "deploy-cwd"
            cwd.mkdir(exist_ok=True)
            command = [str(NODE), str(WRANGLER), "pages", "deploy", str(root / "deploy-public"),
                       "--project-name", PROJECT, "--branch", PRODUCTION_BRANCH, "--commit-hash", head,
                       "--commit-message", "GLP43 approved current package", "--no-bundle"]
            try:
                proc = subprocess.run(command, cwd=str(cwd), env=wrangler_env(run_dir, client.token),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, timeout=1800)
                stages["pages_deploy_cli"] = {"returncode": proc.returncode, "stdout_sha256": sha_bytes(proc.stdout.encode()),
                                               "stderr_sha256": sha_bytes(proc.stderr.encode())}
            except (OSError, subprocess.SubprocessError) as exc:
                stages["pages_deploy_cli"] = {"ambiguous": type(exc).__name__}
            checkpoint(receipt_path, receipt)
        # Same historical no-replay rule: every existing intent reconciles only.
        current = production_deployment(client.project())
        candidates = [d for d in current_deployments(client) if deployment_commit(d) == head
                      and d.get("environment") == "production" and deployment_branch(d) == PRODUCTION_BRANCH]
        if (len(candidates) != 1 or current.get("id") != candidates[0].get("id")
                or deployment_commit(current) != head or not successful_production(current)
                or current.get("id") == rollback):
            fail("current_deploy_outcome_unsettled_reconcile_only")
        deployment_id = current["id"]
        stages["pages_deployment"] = {"deployment_id": deployment_id, "commit_hash": head}
        checkpoint(receipt_path, receipt)
        current_posteffect(args, root, prepared, run_dir, receipt_path, receipt, client, current, lock_fd)
        receipt["status"] = "PASS_PRODUCTION_TERMINAL"
        receipt["effects"] = {"pages_deployments": 1, "pages_rollbacks": 0, "pointer_atomic_cas": 1,
                              "worker_deploys": 0, "d1_mutations": 0, "cron_mutations": 0, "email_sends": 0}
        checkpoint(receipt_path, receipt)
        return {"status": receipt["status"], "receipt": str(receipt_path), "receipt_sha256": sha_file(receipt_path),
                "deployment_id": deployment_id, "site_head": head}
    finally:
        os.close(lock_fd)


def current_posteffect(args: argparse.Namespace, root: Path, prepared: dict[str, Any], run_dir: Path,
                       receipt_path: Path, receipt: dict[str, Any], client: CloudflareClient,
                       current: dict[str, Any], lock_fd: int) -> None:
    # Exact dedented former publish block: same oracle, native CAS and rollback.
    stages, deployment_id, head = receipt["stages"], current["id"], prepared["site_head"]
    try:
        stages["route_probes"] = current_route_matrix(client, current, prepared["route_specs"],
                                                      root / "release/public", receipt_path, receipt)
        stages["runtime_and_holds"] = current_runtime_readback(current, prepared)
        current_prepared(root, args.expected_prepared_manifest_sha256)
        intent = current_pointer_intent(root, prepared, deployment_id, run_dir)
        if "pointer_intent" in stages and stages["pointer_intent"] != intent:
            fail("current_pointer_intent_drift")
        stages["pointer_intent"] = intent
        checkpoint(receipt_path, receipt)
        if production_deployment(client.project()).get("id") != deployment_id:
            fail("current_canonical_changed_before_pointer")
        stages["pointer_cas"] = atomic_pointer_cas(intent, lock_fd, production=True)
        if production_deployment(client.project()).get("id") != deployment_id:
            fail("current_canonical_changed_after_pointer")
        raw = base64.b64decode(intent["successor_bytes_base64"], validate=True)
        _pointer_exact(POINTER, raw, int(intent["final_mode"], 8), intent["final_uid"], intent["final_gid"])
        stages["canonical_pointer_readback"] = {"deployment_id": deployment_id, "site_head": head, "pointer_sha256": sha_bytes(raw)}
    except Exception:
        pointer, _ = capture_pointer(POINTER)
        if json.loads(pointer).get("deployment_id") == deployment_id:
            # Never roll back the provider behind an already advanced pointer.
            receipt["status"] = "BLOCKED_POST_CAS_VALIDATION"
            checkpoint(receipt_path, receipt)
            raise
        if production_deployment(client.project()).get("id") != deployment_id:
            fail("current_foreign_head_no_rollback")
        verify_signed_predecessor(prepared["predecessor_pointer"])
        rollback = prepared["rollback_deployment_id"]
        stages["rollback_intent"] = {"deployment_id": rollback}
        checkpoint(receipt_path, receipt)
        try:
            client.rollback(rollback)
        finally:
            restored = production_deployment(client.project())
            if restored.get("id") == rollback and successful_production(restored):
                verify_signed_predecessor(prepared["predecessor_pointer"])
                receipt["status"] = "BLOCKED_ROLLED_BACK"
                checkpoint(receipt_path, receipt)
        raise


def current_promotion_source(args: argparse.Namespace, prepared: dict[str, Any], path: Path) -> dict[str, Any]:
    # The original consumed upload receipt is input only, never a writable target.
    original = current_json(path, args.expected_rolled_back_receipt_sha256)
    binding = {"prepared_manifest_sha256": args.expected_prepared_manifest_sha256,
               "approval_sha256": args.expected_approval_sha256}
    stages = original.get("stages", {})
    # Select only the maintained exact head/gate/package cohort, as preparation
    # does. Caller IDs and mutable runtime globals cannot adopt a predecessor.
    rollback = current_rollback(prepared["site_head"], prepared["build_receipt"]["sha256"],
                                prepared["package_manifest_sha256"])
    if (not args.authorize_existing_promotion or not re.fullmatch(r"[0-9a-f]{40}", args.site_head)
            or args.site_head != prepared["site_head"] or not DEPLOYMENT_ID.fullmatch(args.candidate_deployment_id)
            or args.candidate_deployment_id == rollback
            or prepared["rollback_deployment_id"] != rollback
            or original.get("schema") != "glp43-current-deploy-receipt-v1"
            or original.get("status") != "BLOCKED_ROLLED_BACK" or original.get("binding") != binding
            or original.get("site_head") != args.site_head or original.get("rollback_deployment_id") != rollback
            or original.get("deploy_directory") != prepared["deploy_directory"]
            or stages.get("pages_deploy_cli", {}).get("returncode") != 0
            or stages.get("pages_deploy_intent", {}).get("commit_hash") != args.site_head
            or stages.get("pages_deploy_intent", {}).get("predecessor_id") != rollback
            or stages.get("pages_deployment") != {"deployment_id": args.candidate_deployment_id, "commit_hash": args.site_head}
            or stages.get("rollback_intent") != {"deployment_id": rollback}
            or "pointer_intent" in stages or "pointer_cas" in stages
            or prepared["predecessor_pointer"].get("deployment_id") != rollback):
        fail("current_promotion_source_or_authorization_mismatch")
    return {**binding, "original_receipt_sha256": args.expected_rolled_back_receipt_sha256,
            "candidate_deployment_id": args.candidate_deployment_id, "site_head": args.site_head,
            "predecessor_id": rollback, "predecessor_pointer_sha256": prepared["predecessor_pointer"]["sha256"]}


# Fixed reviewed forward edges are bound to actual rolled-back journals.
# These are not caller-supplied journal names or a recursive renewal scheme.
CURRENT_PROMOTION_ROOT_SHA256 = "162966b57bf902f14dce9dc43a13d3c639f77af4f35fd7f375dadbda3bb7d5e6"
CURRENT_PROMOTION_ROLLED_BACK_SUCCESSOR_SHA256 = "3d00967317267cb5b7bbb0925ad86d0809c4fd70cd6a3b25b44dc8548226377b"
# One reviewed post-identity continuation, not a generic recursive renewal.
CURRENT_PROMOTION_POST_IDENTITY_TERMINAL_SHA256 = "501331e3ab5fed5742672960d10cf1ca3dad0e59d5c2176d012e7636b131b8b4"

def current_promotion_terminal_source(args: argparse.Namespace, binding: dict[str, Any],
                                      original_path: Path, prior_path: Path,
                                      *, expected_sha: str | None = None) -> dict[str, Any]:
    # Original terminal -> first successor, or the fixed authenticated second edge.
    prior_sha = expected_sha if expected_sha is not None else args.expected_terminal_promotion_sha256
    if (not args.authorize_renewed_promotion or not isinstance(prior_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", prior_sha)):
        fail("current_promotion_renewal_authorization_missing")
    prior = current_json(prior_path, prior_sha)
    stages, effects = prior.get("stages"), prior.get("effects")
    selection = {"deployment_id": args.candidate_deployment_id, "commit_hash": args.site_head,
                 "predecessor_id": binding["predecessor_id"],
                 "original_receipt_sha256": args.expected_rolled_back_receipt_sha256,
                 "effect": "select_existing_successful_production_deployment"}
    if (prior.get("schema") != "glp43-current-promotion-receipt-v1"
            or prior.get("status") != "BLOCKED_ROLLED_BACK" or prior.get("binding") != binding
            or prior.get("original_receipt") != str(original_path) or prior.get("site_head") != args.site_head
            or not isinstance(stages, dict) or not isinstance(effects, dict)
            or stages.get("existing_candidate_selection_intent") != selection
            or stages.get("existing_candidate_selection_result") != {"returned": True}
            or stages.get("existing_candidate_selection_confirmed") != {
                "deployment_id": args.candidate_deployment_id, "commit_hash": args.site_head}
            or stages.get("rollback_intent") != {"deployment_id": binding["predecessor_id"]}
            or "pointer_intent" in stages or "pointer_cas" in stages
            or effects != {
                "pages_deployments": 0, "existing_candidate_selection_post_intents": 1,
                "existing_candidate_selections_confirmed": 1, "guarded_rollback_post_intents": 1,
                "guarded_rollbacks_confirmed": 1, "pointer_atomic_cas": 0}):
        fail("current_promotion_terminal_preimage_mismatch")
    return {**binding, "terminal_promotion_sha256": prior_sha}

def current_promotion_rolled_back_successor_source(args: argparse.Namespace, binding: dict[str, Any],
                                                    original_path: Path, root_path: Path,
                                                    prior_path: Path) -> dict[str, Any]:
    if (not args.authorize_rolled_back_successor_renewal
            or args.expected_terminal_promotion_sha256 != CURRENT_PROMOTION_ROLLED_BACK_SUCCESSOR_SHA256):
        fail("current_promotion_second_renewal_authorization_missing")
    root_binding = current_promotion_terminal_source(
        args, binding, original_path, root_path, expected_sha=CURRENT_PROMOTION_ROOT_SHA256)
    return current_promotion_terminal_source(args, root_binding, original_path, prior_path)


def current_promotion_post_identity_source(args: argparse.Namespace, binding: dict[str, Any],
                                           original_path: Path, root_path: Path,
                                           first_path: Path, second_path: Path) -> dict[str, Any]:
    if (not args.authorize_post_identity_promotion or not args.authorize_rolled_back_successor_renewal
            or args.expected_terminal_promotion_sha256 != CURRENT_PROMOTION_POST_IDENTITY_TERMINAL_SHA256):
        fail("current_promotion_post_identity_authorization_missing")
    root_binding = current_promotion_terminal_source(
        args, binding, original_path, root_path, expected_sha=CURRENT_PROMOTION_ROOT_SHA256)
    first_binding = current_promotion_terminal_source(
        args, root_binding, original_path, first_path,
        expected_sha=CURRENT_PROMOTION_ROLLED_BACK_SUCCESSOR_SHA256)
    return current_promotion_terminal_source(args, first_binding, original_path, second_path)

def run_promote_current_existing(args: argparse.Namespace) -> dict[str, Any]:
    root = package_plain_path(Path(args.prepared_dir))
    prepared = current_prepared(root, args.expected_prepared_manifest_sha256)
    current_approval(args, prepared)
    run_dir = SEO_REPORT_ROOT / ("current-release-" + prepared["site_head"])
    original_path = run_dir / "CURRENT-RECEIPT.json"
    binding = current_promotion_source(args, prepared, original_path)
    rollback = binding["predecessor_id"]
    renewing = (args.authorize_renewed_promotion or args.expected_terminal_promotion_sha256 is not None
                or args.authorize_rolled_back_successor_renewal or args.authorize_post_identity_promotion)
    third_renewal = args.authorize_post_identity_promotion
    second_renewal = args.authorize_rolled_back_successor_renewal
    root_path = run_dir / ("CURRENT-PROMOTION-" + args.candidate_deployment_id + ".json")
    first_path = run_dir / ("CURRENT-PROMOTION-" + args.candidate_deployment_id + "-SUCCESSOR-"
                            + CURRENT_PROMOTION_ROOT_SHA256 + ".json")
    prior_path = (run_dir / ("CURRENT-PROMOTION-" + args.candidate_deployment_id + "-SUCCESSOR-"
                             + CURRENT_PROMOTION_ROLLED_BACK_SUCCESSOR_SHA256 + ".json") if third_renewal
                  else first_path if second_renewal else root_path)
    if third_renewal:
        binding = current_promotion_post_identity_source(args, binding, original_path, root_path, first_path, prior_path)
    elif second_renewal:
        binding = current_promotion_rolled_back_successor_source(args, binding, original_path, root_path, prior_path)
    elif renewing:
        binding = current_promotion_terminal_source(args, binding, original_path, prior_path)
    verify_immutable_tree(TOOLCHAIN, CURRENT_TOOLCHAIN, toolchain=True)
    lock_fd = acquire_lock()
    try:
        def recheck_source() -> None:
            source = current_promotion_source(args, prepared, original_path)
            if third_renewal:
                current_promotion_post_identity_source(args, source, original_path, root_path, first_path, prior_path)
            elif second_renewal:
                current_promotion_rolled_back_successor_source(args, source, original_path, root_path, prior_path)
            elif renewing:
                current_promotion_terminal_source(args, source, original_path, prior_path)

        recheck_source()  # full root and prior lineage while holding the native lock
        deployment_id, head = args.candidate_deployment_id, args.site_head
        receipt_path = (run_dir / ("CURRENT-PROMOTION-" + deployment_id + "-SUCCESSOR-"
                                   + binding["terminal_promotion_sha256"] + ".json") if renewing else prior_path)
        selection = {"deployment_id": deployment_id, "commit_hash": head, "predecessor_id": rollback,
                     "original_receipt_sha256": args.expected_rolled_back_receipt_sha256,
                     "effect": "select_existing_successful_production_deployment"}
        recheck_source()
        client = CloudflareClient(read_token())
        if receipt_path.exists():
            require_regular(package_plain_path(receipt_path))
            receipt = load_json(receipt_path)
            if (receipt.get("schema") != "glp43-current-promotion-receipt-v1" or receipt.get("binding") != binding
                    or receipt.get("status") != "ACTIVE"
                    or receipt.get("stages", {}).get("existing_candidate_selection_intent") != selection):
                fail("current_promotion_consumed_or_binding_changed")
        else:
            candidate = client.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments/{deployment_id}")
            current = production_deployment(client.project())
            if (not isinstance(candidate, dict) or candidate.get("id") != deployment_id
                    or deployment_commit(candidate) != head or not successful_production(candidate)
                    or current.get("id") != rollback or not successful_production(current)):
                fail("current_promotion_predecessor_or_candidate_mismatch")
            current_prepared(root, args.expected_prepared_manifest_sha256)
            current_approval(args, prepared)
            recheck_source()
            verify_signed_predecessor(prepared["predecessor_pointer"])
            receipt = {"schema": "glp43-current-promotion-receipt-v1", "status": "ACTIVE", "binding": binding,
                       "original_receipt": str(original_path), "site_head": head,
                       "stages": {"existing_candidate_selection_intent": selection},
                       "effects": {"pages_deployments": 0, "existing_candidate_selection_post_intents": 1,
                                   "existing_candidate_selections_confirmed": None, "guarded_rollback_post_intents": 0,
                                   "guarded_rollbacks_confirmed": 0, "pointer_atomic_cas": 0}}
            exclusive_file(receipt_path, json_bytes(receipt))
            # Only this exclusive creation permits the one selection POST.
            # Cloudflare documents /rollback as selecting a prior successful
            # production deployment; it uploads/builds nothing.
            immediate = production_deployment(client.project())
            if immediate.get("id") != rollback or not successful_production(immediate):
                fail("current_canonical_changed_before_promotion")
            current_prepared(root, args.expected_prepared_manifest_sha256)
            current_approval(args, prepared)
            recheck_source()
            if renewing:
                # An old candidate read is not a license for another POST.
                candidate = client.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments/{deployment_id}")
                if (not isinstance(candidate, dict) or candidate.get("id") != deployment_id
                        or deployment_commit(candidate) != head or not successful_production(candidate)):
                    fail("current_promotion_candidate_changed_before_selection")
            verify_signed_predecessor(prepared["predecessor_pointer"])
            try:
                client.rollback(deployment_id)
                receipt["stages"]["existing_candidate_selection_result"] = {"returned": True}
            except Exception as exc:
                receipt["stages"]["existing_candidate_selection_result"] = {"ambiguous": type(exc).__name__}
            checkpoint(receipt_path, receipt)
        stages = receipt["stages"]
        try:
            current = production_deployment(client.project())
            if "rollback_intent" in stages:
                # An uncertain guarded rollback also cannot be repeated.
                if current.get("id") == rollback and successful_production(current):
                    verify_signed_predecessor(prepared["predecessor_pointer"])
                    receipt["status"] = "BLOCKED_ROLLED_BACK"
                fail("current_prior_rollback_no_replay")
            if (current.get("id") != deployment_id or deployment_commit(current) != head
                    or not successful_production(current)):
                fail("current_promotion_outcome_unsettled_reconcile_only")
            stages["existing_candidate_selection_confirmed"] = {"deployment_id": deployment_id, "commit_hash": head}
            checkpoint(receipt_path, receipt)
            current_posteffect(args, root, prepared, run_dir, receipt_path, receipt, client, current, lock_fd)
            receipt["status"] = "PASS_PRODUCTION_TERMINAL"
        finally:
            # Intent counts are upper bounds on POSTs, not fabricated outcomes.
            # Unknown completion remains null until canonical/CAS readback.
            receipt["effects"] = {"pages_deployments": 0, "existing_candidate_selection_post_intents": 1,
                                  "existing_candidate_selections_confirmed": 1 if "existing_candidate_selection_confirmed" in stages else None,
                                  "guarded_rollback_post_intents": int("rollback_intent" in stages),
                                  "guarded_rollbacks_confirmed": (1 if receipt["status"] == "BLOCKED_ROLLED_BACK"
                                                                 else None if "rollback_intent" in stages else 0),
                                  "pointer_atomic_cas": 1 if "pointer_cas" in stages else None if "pointer_intent" in stages else 0}
            checkpoint(receipt_path, receipt)
        return {"status": receipt["status"], "receipt": str(receipt_path), "receipt_sha256": sha_file(receipt_path),
                "deployment_id": deployment_id, "site_head": head}
    finally:
        os.close(lock_fd)


def current_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="GLP43 exact current package: offline prepare OR explicitly approved production consumption")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare-current", action="store_true")
    modes.add_argument("--publish-current", action="store_true")
    modes.add_argument("--promote-current-existing", action="store_true")
    parser.add_argument("--prepared-dir", required=True)
    preparing = "--prepare-current" in argv
    flags = (("source-root", "site-head", "git-tree", "build-seat", "package-dir", "expected-package-manifest-sha256",
              "build-receipt", "expected-build-receipt-sha256") if preparing else
             ("expected-prepared-manifest-sha256", "approval", "expected-approval-sha256"))
    for flag in flags:
        parser.add_argument("--" + flag, required=True)
    if not preparing:
        parser.add_argument("--authorize-production", action="store_true")
    if "--promote-current-existing" in argv:
        for flag in ("site-head", "candidate-deployment-id", "expected-rolled-back-receipt-sha256"):
            parser.add_argument("--" + flag, required=True)
        parser.add_argument("--authorize-existing-promotion", action="store_true")
        parser.add_argument("--authorize-renewed-promotion", action="store_true")
        parser.add_argument("--authorize-rolled-back-successor-renewal", action="store_true")
        parser.add_argument("--authorize-post-identity-promotion", action="store_true")
        parser.add_argument("--expected-terminal-promotion-sha256")
    args = parser.parse_args(argv)
    try:
        runner = (run_prepare_current if preparing else run_promote_current_existing
                  if args.promote_current_existing else run_publish_current)
        result = runner(args)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (GateError, OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.SubprocessError) as exc:
        error = str(exc) if isinstance(exc, GateError) else type(exc).__name__
        print(json.dumps({"status": "BLOCK", "error": error}), file=sys.stderr)
        return 1
