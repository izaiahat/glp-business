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
    if not isinstance(receipt, dict) or receipt.get("schema") != "glp-d1-release-result-v1" or receipt.get("status") != "PASS":
        fail("package_build_receipt_invalid")
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
    if "--prepare-current" in argv or "--publish-current" in argv:
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
CURRENT_ROLLBACK = "ca0251a2-dd4e-469c-be3b-79ee8805c5f3"
CURRENT_REPOSITORY = "izaiahat/glp-site-candidate"
CURRENT_TOOLCHAIN = {"closure_sha256": "1af0afc423948202725ae9299b4803c46b68cbb053358943a17123c6dfbc4ad7",
                     "file_count": 1662, "directory_count": 258, "total_bytes": 621665046}
CURRENT_AUTHORITIES = {
    "ops/linear/approvals/OPERATOR-DECISION-20260926-release-target-root-195.md":
        "f9ff3e92029b5fd6a3582ca7e7dc64fad9e699adc2b28f0b842d62d17a7870ec",
    "ops/linear/approvals/OPERATOR-DECISION-20260926-release-review-astra.md":
        "7d0d49def4254a19e088d4d6586c9963325311da17cfbd6054dd175876240fb0",
}


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
    if closures["functions"]["file_count"] != 17:
        fail("current_functions_inventory_drift")
    for row in gate.get("authority_files", []):
        if row.get("file") not in ("data/go-runtime-selection.json", "data/go-runtime-promotion-authority.json",
                                    "data/tracking-allowed-hosts.json", "artifacts/ops/build-report.json"):
            fail("current_gate_authority_path_invalid")
        require_regular(package_plain_path(source / row["file"]))
        if sha_file(source / row["file"]) != row["sha256"]:
            fail("current_gate_authority_drift")
    return {"git_tree": tree, "built_source_sha256": digest, "tracked_files": rows,
            "generated_diff_paths": changed, "generated_diff_sha256": sha_bytes(diff),
            "runtime": closures, "authority_files": gate["authority_files"],
            "go_manifest_sha256": sha_file(source / "functions/go/program-manifest.js"),
            "wrangler_config_sha256": sha_file(source / "wrangler.toml")}


def current_pointer() -> dict[str, Any]:
    raw, st = capture_pointer(POINTER)
    parsed = json.loads(raw)
    if parsed.get("deployment_id") != CURRENT_ROLLBACK:
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
    # Same exact comment-only canonicalization as the historical Pages probe.
    fixed, count = re.subn(rb"// \.\./\.wrangler/tmp/pages-[^/\r\n]+/functionsRoutes-[^\r\n]+",
                          b"// r13-canonical-functions-routes.mjs", raw)
    if count != 1:
        fail("current_wrangler_nondeterminism_shape_drift")
    atomic_file(path, fixed)
    return {"argv": command, "returncode": proc.returncode,
            "stdout_sha256": sha_bytes(proc.stdout.encode()), "stderr_sha256": sha_bytes(proc.stderr.encode()),
            "raw_worker_sha256": sha_bytes(raw), "worker_sha256": sha_bytes(fixed),
            "pages_random_comment_replacements": count, "routes": current_routes(fixed)}


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
    if (manifest.get("schema") != "glp34-deploy-package-v1" or manifest.get("site_head") != args.site_head
            or manifest.get("site_repository") != CURRENT_REPOSITORY or manifest.get("project") != PROJECT
            or manifest.get("account_id") != ACCOUNT_ID or manifest.get("rollback_branch") != PRODUCTION_BRANCH
            or manifest.get("rollback_deployment_id") != CURRENT_ROLLBACK
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
    pointer = current_pointer()
    verify_immutable_tree(TOOLCHAIN, CURRENT_TOOLCHAIN, toolchain=True)
    subprocess.run([sys.executable, str(BUSINESS / "scripts/candidate_build_lock.py"),
                    "require-held", "--seat", args.build_seat], check=True, capture_output=True, timeout=30)
    package_site_commit(args.site_head)
    token = read_token()
    package_rollback(CURRENT_ROLLBACK, token, detail=True)
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
                    "toolchain": CURRENT_TOOLCHAIN, "rollback_deployment_id": CURRENT_ROLLBACK,
                    "predecessor_pointer": pointer, "release": file_closure(release),
                    "deploy_directory": file_closure(deploy), "holds": holds, "route_specs": route_specs,
                    "provider_reads": 3, "provider_mutations": 0, "release_authorized": False}
        package_rollback(CURRENT_ROLLBACK, token, detail=False)
        verify_signed_predecessor(pointer)
        if current_source(source, args.site_head, args.git_tree, gate) != binding or package_tree(public) != (tree_hash, rows):
            fail("current_inputs_changed_before_completion")
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
    if (value.get("schema") != "glp43-current-prepared-v1" or value.get("prepared_dir") != str(root)
            or value.get("site_repository") != CURRENT_REPOSITORY or value.get("project") != PROJECT
            or value.get("branch") != PRODUCTION_BRANCH or value.get("account_id") != ACCOUNT_ID
            or value.get("rollback_deployment_id") != CURRENT_ROLLBACK or value.get("toolchain") != CURRENT_TOOLCHAIN):
        fail("current_prepared_identity_mismatch")
    if (file_closure(root / "release") != value["release"]
            or file_closure(root / "deploy-public") != value["deploy_directory"]
            or sha_file(root / "deploy-public/_worker.js") != value["compiled"]["worker_sha256"]):
        fail("current_prepared_bytes_drift")
    package = current_json(root / "PACKAGE-MANIFEST.json", value["package_manifest_sha256"])
    gate = current_json(root / "gate.json", value["build_receipt"]["sha256"])
    if (package_tree(root / "release/public") != (value["public_tree_sha256"], package["files"])
            or current_route_specs(root / "release/public") != value["route_specs"]):
        fail("current_prepared_public_drift")
    return value


def current_approval(args: argparse.Namespace, prepared: dict[str, Any]) -> dict[str, Any]:
    review = current_json(Path(args.approval), args.expected_approval_sha256)
    if (review.get("schema") != "glp42-designated-astra-release-review-v1"
            or review.get("verdict") != "APPROVE" or review.get("release_accepted") is not True
            or review.get("blocking_findings") != []):
        fail("current_release_not_approved")
    reviewer = review.get("reviewer", {})
    if (reviewer.get("model") != "gpt-6-astra-900k" or reviewer.get("provider") != "openai-codex"
            or "not an author" not in reviewer.get("role", "")
            or reviewer.get("authority") != list(CURRENT_AUTHORITIES)[1]
            or reviewer.get("original_target_authority") != list(CURRENT_AUTHORITIES)[0]):
        fail("current_designated_independent_reviewer_required")
    required = {"repository": CURRENT_REPOSITORY, "head": prepared["site_head"],
                "git_tree": prepared["source"]["git_tree"], "public_tree_sha256": prepared["public_tree_sha256"],
                "package_manifest_sha256": prepared["package_manifest_sha256"],
                "project": PROJECT, "rollback_branch": PRODUCTION_BRANCH, "rollback_environment": "production",
                "rollback_deployment_id": CURRENT_ROLLBACK,
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
    paths = ["/best/online-glp-1-providers/", "/api/postback/affiliati", "/providers/?glp43=readback"] + ["/go/" + row["id"] for row in prepared["holds"]["held"]]
    results = []
    for base in bases:
        for path in paths:
            if not re.fullmatch(r"/(?:best/online-glp-1-providers/|go/[a-z0-9-]+|api/postback/affiliati|providers/\?glp43=readback)", path):
                fail("current_runtime_probe_path_invalid")
            method = "GET" if path.startswith("/best/") else "HEAD"
            request = urllib.request.Request(base + path, method=method, headers={"Cache-Control": "no-cache"})
            try:
                response = urllib.request.build_opener(CurrentNoRedirect()).open(request, timeout=30)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status_code, headers = response.code, response.headers
            if path.startswith("/go/"):
                ok = (status_code == 307 and headers.get("Location") == SITE + "/best/online-glp-1-providers/"
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
                      "supersedes": {"deployment_id": CURRENT_ROLLBACK, "release_root": spec["release_root"]}})
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


def run_publish_current(args: argparse.Namespace) -> dict[str, Any]:
    root = package_plain_path(Path(args.prepared_dir))
    prepared = current_prepared(root, args.expected_prepared_manifest_sha256)
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
                       "site_head": prepared["site_head"], "rollback_deployment_id": CURRENT_ROLLBACK,
                       "deploy_directory": prepared["deploy_directory"], "stages": {}}
            exclusive_file(receipt_path, json_bytes(receipt))
        stages = receipt["stages"]
        client = CloudflareClient(read_token())
        head = prepared["site_head"]
        current = production_deployment(client.project())
        candidates = [d for d in client.deployments() if deployment_commit(d) == head
                      and d.get("environment") == "production" and deployment_branch(d) == PRODUCTION_BRANCH]
        if "rollback_intent" in stages:
            # An uncertain rollback is also reconciliation-only: never replay it.
            if current.get("id") == CURRENT_ROLLBACK and successful_production(current):
                verify_signed_predecessor(prepared["predecessor_pointer"])
                receipt["status"] = "BLOCKED_ROLLED_BACK"
                checkpoint(receipt_path, receipt)
            fail("current_prior_rollback_no_replay")
        if "pages_deploy_intent" not in stages:
            if current.get("id") != CURRENT_ROLLBACK or not successful_production(current) or candidates:
                fail("current_predecessor_or_existing_candidate_mismatch")
            current_prepared(root, args.expected_prepared_manifest_sha256)
            current_approval(args, prepared)
            verify_signed_predecessor(prepared["predecessor_pointer"])
            stages["pages_deploy_intent"] = {"predecessor_id": CURRENT_ROLLBACK, "commit_hash": head, "created_at_utc": utc()}
            checkpoint(receipt_path, receipt)
            immediate = production_deployment(client.project())
            if immediate.get("id") != CURRENT_ROLLBACK or not successful_production(immediate):
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
        candidates = [d for d in client.deployments() if deployment_commit(d) == head
                      and d.get("environment") == "production" and deployment_branch(d) == PRODUCTION_BRANCH]
        if (len(candidates) != 1 or current.get("id") != candidates[0].get("id")
                or deployment_commit(current) != head or not successful_production(current)
                or current.get("id") == CURRENT_ROLLBACK):
            fail("current_deploy_outcome_unsettled_reconcile_only")
        deployment_id = current["id"]
        stages["pages_deployment"] = {"deployment_id": deployment_id, "commit_hash": head}
        checkpoint(receipt_path, receipt)
        try:
            stages["route_probes"] = route_matrix(client, current, prepared["route_specs"])
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
            stages["rollback_intent"] = {"deployment_id": CURRENT_ROLLBACK}
            checkpoint(receipt_path, receipt)
            try:
                client.rollback(CURRENT_ROLLBACK)
            finally:
                restored = production_deployment(client.project())
                if restored.get("id") == CURRENT_ROLLBACK and successful_production(restored):
                    verify_signed_predecessor(prepared["predecessor_pointer"])
                    receipt["status"] = "BLOCKED_ROLLED_BACK"
                    checkpoint(receipt_path, receipt)
            raise
        receipt["status"] = "PASS_PRODUCTION_TERMINAL"
        receipt["effects"] = {"pages_deployments": 1, "pages_rollbacks": 0, "pointer_atomic_cas": 1,
                              "worker_deploys": 0, "d1_mutations": 0, "cron_mutations": 0, "email_sends": 0}
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
    parser.add_argument("--prepared-dir", required=True)
    preparing = "--prepare-current" in argv
    flags = (("source-root", "site-head", "git-tree", "build-seat", "package-dir", "expected-package-manifest-sha256",
              "build-receipt", "expected-build-receipt-sha256") if preparing else
             ("expected-prepared-manifest-sha256", "approval", "expected-approval-sha256"))
    for flag in flags:
        parser.add_argument("--" + flag, required=True)
    if not preparing:
        parser.add_argument("--authorize-production", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_prepare_current(args) if preparing else run_publish_current(args)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (GateError, OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.SubprocessError) as exc:
        error = str(exc) if isinstance(exc, GateError) else type(exc).__name__
        print(json.dumps({"status": "BLOCK", "error": error}), file=sys.stderr)
        return 1
