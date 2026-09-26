# Maintained package-only extension, embedded by generate_glp34_deploy_carrier.py.
# No authority, signature, deploy, lock, pointer, or write-capable client is used.

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


def run_package_only(args: argparse.Namespace) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", args.site_head):
        fail("package_site_head_invalid")
    if not HEX64.fullmatch(args.expected_tree_sha256):
        fail("package_expected_tree_invalid")
    if not DEPLOYMENT_ID.fullmatch(args.rollback_deployment_id):
        fail("package_rollback_id_invalid")
    public = package_plain_path(Path(args.public_root))
    output = package_plain_path(Path(args.package_dir))
    if output == public or public in output.parents or output in public.parents:
        fail("package_output_overlaps_input")
    if output.exists() or not output.parent.is_dir():
        fail("package_output_must_be_new_with_existing_parent")
    tree_hash, rows = package_tree(public)
    if tree_hash != args.expected_tree_sha256:
        fail("package_public_tree_mismatch")
    token = read_token()
    package_rollback(args.rollback_deployment_id, token, detail=True)
    # First local write occurs only after exact tree + current rollback checks.
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
        manifest = {
            "schema": "glp34-deploy-package-v1", "mode": "package-only",
            "site_head": args.site_head, "site_head_binding": "operator-supplied-not-build-attestation",
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
        # Manifest is the completion marker, never emitted for a mismatching copy.
        exclusive_file(output / "PACKAGE-MANIFEST.json", json_bytes(manifest), mode=0o444)
        return {"status": "PASS_PACKAGE_ONLY", "package_dir": str(output),
                "manifest_sha256": sha_file(output / "PACKAGE-MANIFEST.json"),
                "public_tree_sha256": tree_hash, "file_count": len(rows),
                "provider_reads": 3, "provider_mutations": 0, "release_authorized": False}
    except BaseException:
        shutil.rmtree(output)
        raise


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--package-only" not in argv:
        return legacy_main(argv)
    parser = argparse.ArgumentParser(description="Package public bytes; no release authorization or deployment")
    parser.add_argument("--package-only", action="store_true", required=True)
    for flag in ("site-head", "public-root", "expected-tree-sha256", "rollback-deployment-id", "package-dir"):
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
