#!/usr/bin/env node
import { access, readFile, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

function parseArgs(argv) {
  const args = { release: "" };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--release") args.release = argv[++index] || "";
    else throw new Error(`unknown_argument:${arg}`);
  }
  if (!args.release) throw new Error("release_required");
  return args;
}

async function readJson(filePath) {
  return JSON.parse(await readFile(filePath, "utf8"));
}

async function atomicWrite(filePath, text) {
  const temporary = `${filePath}.b2-refresh-${process.pid}`;
  try {
    await writeFile(temporary, text, { encoding: "utf8", mode: 0o644 });
    await rename(temporary, filePath);
  } finally {
    await rm(temporary, { force: true });
  }
}

async function main() {
  const { release: releaseArg } = parseArgs(process.argv.slice(2));
  const release = path.resolve(releaseArg);
  const registryPath = path.join(release, "data", "program-registry.json");
  const ledgerPath = path.join(release, "data", "program-content-ledger.json");
  const stagedManifestPath = path.join(release, "artifacts", "ops", "worker-manifest.staging.json");
  const runtimePath = path.join(release, "functions", "go", "program-manifest.js");
  await Promise.all([
    access(registryPath), access(ledgerPath), access(stagedManifestPath), access(runtimePath),
  ]);

  const [
    { deriveGoEntriesFromRegistry },
    { renderGoRuntimeManifestModule, manifestSha256 },
    { loadAffiliateLinkMap },
    { loadGoPromotionAuthority },
  ] = await Promise.all([
    import(pathToFileURL(path.join(release, "src", "program-registry.mjs")).href),
    import(pathToFileURL(path.join(release, "src", "go-runtime-manifest.mjs")).href),
    import(pathToFileURL(path.join(release, "src", "affiliate-link-map.mjs")).href),
    import(pathToFileURL(path.join(release, "src", "go-runtime-selection.mjs")).href),
  ]);
  const [registry, ledger, stagedManifest] = await Promise.all([
    readJson(registryPath), readJson(ledgerPath), readJson(stagedManifestPath),
  ]);
  const siteProfile = "glp-clinic-scout";
  if (registry?.schema !== "site-program-registry-v1") throw new Error("program_registry_invalid");
  if (!registry?.site_profiles?.[siteProfile]) throw new Error(`site_profile_unknown:${siteProfile}`);
  if (!Array.isArray(ledger?.programs)) throw new Error("content_ledger_programs_invalid");
  if (stagedManifest?.schema !== "affiliate-worker-staging-v3-registry") {
    throw new Error("staged_go_manifest_invalid");
  }
  // Render uses the adopted selection overlay as well as the raw network
  // authority. Read the same governed projection as build-staging, not the
  // raw file alone, or selected /go rows are silently re-frozen as held.
  const goPromotionAuthority = loadGoPromotionAuthority(release);

  if (stagedManifest?.site_profile !== siteProfile) throw new Error("staged_go_manifest_profile_drift");
  if (!String(stagedManifest?.internal_fallback || "").startsWith("https://glpclinicscout.com/")) {
    throw new Error("staged_go_manifest_fallback_invalid");
  }

  const manifest = {
    schema: "affiliate-worker-staging-v3-registry",
    program_registry_schema: registry.schema,
    site_profile: siteProfile,
    // Preserve the candidate's governed fallback instead of maintaining a
    // second literal. build-staging re-derives this same manifest and fails
    // closed if these bytes drift.
    internal_fallback: stagedManifest.internal_fallback,
    // DEFECT 2026-09-02 (deploy-fable-20260901). The /go contract support adds a
    // 5th parameter, goPromotionAuthority, to deriveGoEntriesFromRegistry and
    // derives live_link_allowed / promotion_authorized / status from it. This
    // refresh omitted that argument, so it always regenerated the manifest with
    // an empty authority - every entry unpromoted. It runs AFTER
    // apply_go_runtime_promotion_authority, so it silently reverted the whole
    // promotion and the deploy then failed its own gate with
    // go_runtime_promotion_zero_active. Pass the candidate's authority when the
    // file exists; absent it, the {} default preserves the previous behaviour.
    programs: deriveGoEntriesFromRegistry(
      registry,
      siteProfile,
      ledger.programs,
      loadAffiliateLinkMap("compare", siteProfile),
      goPromotionAuthority,
    ),
  };
  const expected = renderGoRuntimeManifestModule(manifest);
  const before = await readFile(runtimePath, "utf8");
  if (before !== expected) await atomicWrite(runtimePath, expected);
  const after = await readFile(runtimePath, "utf8");
  if (after !== expected) throw new Error("go_runtime_manifest_refresh_readback_mismatch");

  process.stdout.write(`${JSON.stringify({
    schema: "glp-go-runtime-manifest-refresh-v1",
    status: before === expected ? "PASS_ALREADY_CURRENT" : "PASS_REFRESHED",
    release,
    manifest_sha256: manifestSha256(manifest),
    program_count: manifest.programs.length,
  })}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error?.stack || error}\n`);
  process.exitCode = 1;
});
