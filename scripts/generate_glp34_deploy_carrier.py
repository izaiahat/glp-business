#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, importlib.util, os, stat
from pathlib import Path
ROOT=Path('/home/ubuntu/business')
SOURCE=ROOT/'scripts/glp_deploy_build6_authority_v1.py'
SOURCE_SHA256='e54a4af3166e7756f1460283b346a4c76c000cdcde30af7b2d22672b5960366d'
SOURCE_TEST=ROOT/'tests/test_glp_deploy_build6_authority_v1.py'
SOURCE_TEST_SHA256='274b602a2cf6871b91eaf6877fedba81924eb83800e189278db80feb22cb2599'
STAGED_TEST_SHA256='870f6eee47f5aec74c730115fa2d41b6dfd5719be957bf6729e48010758d7565'
TARGET=ROOT/'scripts/glp_deploy_glp34_authority_v1.py'
TARGET_TEST=ROOT/'tests/test_glp_deploy_glp34_authority_v1.py'
AUTH=ROOT/'reports/glp-clinic-scout/growth/glp34/production-authority-20260918-r1'
CAND=ROOT/'site-system/flagship-v2/candidates/glp34-provider-families-20260912'
REL=ROOT/'site-system/flagship-v2/releases/glp34-provider-families-20260918-r1'

def source_module(*, snapshot=False):
 path=Path(__file__).resolve().parent/SOURCE.name if snapshot else SOURCE
 st=path.lstat()
 if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1: raise SystemExit('source_not_regular')
 if not snapshot and (st.st_uid!=0 or st.st_gid!=0 or stat.S_IMODE(st.st_mode)!=0o444): raise SystemExit('source_not_root_sealed')
 source_bytes=path.read_bytes()
 if hashlib.sha256(source_bytes).hexdigest()!=SOURCE_SHA256: raise SystemExit('source_lineage_bytes_drift')
 spec=importlib.util.spec_from_file_location('glp34_generation_lineage',SOURCE)
 if spec is None or spec.loader is None: raise SystemExit('source_loader_missing')
 module=importlib.util.module_from_spec(spec); exec(compile(source_bytes,str(SOURCE),'exec'),module.__dict__)
 return module,source_bytes.decode('utf-8')

def frozen(module,root:Path):
 dirs,files=module._walk_plain(root)
 for p in [root,*dirs,*files]:
  st=p.lstat(); mode=0o555 if stat.S_ISDIR(st.st_mode) else 0o444
  if st.st_uid!=0 or st.st_gid!=0 or stat.S_IMODE(st.st_mode)!=mode: raise SystemExit(f'not_frozen:{p}')
  if stat.S_ISREG(st.st_mode) and st.st_nlink!=1: raise SystemExit(f'not_single_regular:{p}')
 return module.file_closure(root)

def authenticated_source_test(*, snapshot=False):
 path=Path(__file__).resolve().parents[1]/'tests'/SOURCE_TEST.name if snapshot else SOURCE_TEST
 st=path.lstat()
 if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1: raise SystemExit('source_test_not_regular')
 if not snapshot and (st.st_uid!=0 or st.st_gid!=0 or stat.S_IMODE(st.st_mode)!=0o444): raise SystemExit('source_test_not_root_sealed')
 raw=path.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=SOURCE_TEST_SHA256: raise SystemExit('source_test_lineage_bytes_drift')
 return raw.decode('utf-8')

def existing(path:Path,expected:bytes,*,allow_staged=False):
 try: st=path.lstat()
 except FileNotFoundError: return False
 if (not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or st.st_gid!=os.getegid()
     or stat.S_IMODE(st.st_mode)!=0o644 or st.st_nlink!=1):
  raise SystemExit(f'target_custody_drift:{path}')
 raw=path.read_bytes()
 staged=(allow_staged and path==TARGET_TEST and hashlib.sha256(raw).hexdigest()==STAGED_TEST_SHA256)
 if raw!=expected and not staged: raise SystemExit(f'target_bytes_unknown:{path}')
 after=path.lstat()
 if (st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size)!=(after.st_dev,after.st_ino,after.st_mtime_ns,after.st_size):
  raise SystemExit(f'target_changed_during_read:{path}')
 return 'staged' if raw!=expected else 'exact'

def atomic(path:Path,text:str,mode:int=0o644):
 if path.parent.is_symlink() or not path.parent.is_dir(): raise SystemExit(f'target_parent_invalid:{path.parent}')
 tmp=path.parent/('.'+path.name+'.tmp')
 fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_CLOEXEC,mode)
 try:
  view=memoryview(text.encode())
  while view:
   written=os.write(fd,view)
   if written<=0: raise SystemExit('short_write')
   view=view[written:]
  os.fsync(fd)
 finally: os.close(fd)
 try:
  os.link(tmp,path,follow_symlinks=False)
 finally:
  tmp.unlink()
 dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY); os.fsync(dfd); os.close(dfd)
def upgrade_staged_test(path:Path,text:str):
 # Only the already generated, pinned predecessor may be replaced.
 if existing(path,text.encode(),allow_staged=True)!='staged': raise SystemExit('staged_test_changed')
 old=path.lstat()
 tmp=path.parent/('.'+path.name+'.upgrade.tmp')
 fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_CLOEXEC,0o644)
 try:
  data=text.encode()
  while data:
   n=os.write(fd,data)
   if n<=0: raise SystemExit('short_write')
   data=data[n:]
  os.fsync(fd)
 finally: os.close(fd)
 try:
  current=path.lstat()
  if (current.st_dev,current.st_ino,current.st_mtime_ns)!=(old.st_dev,old.st_ino,old.st_mtime_ns) or existing(path,text.encode(),allow_staged=True)!='staged': raise SystemExit('staged_test_changed')
  os.replace(tmp,path)
 finally: tmp.unlink(missing_ok=True)
 dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY); os.fsync(dfd); os.close(dfd)
def main():
 global TARGET,TARGET_TEST
 parser=argparse.ArgumentParser(description='Generate or reconcile pinned GLP34 carrier and test')
 parser.add_argument('--prepare',action='store_true',help='offline source/test preparation only; no twin checks or carrier execution')
 parser.add_argument('--emit-dir',type=Path,help='with --prepare only: emit to a NEW directory from hash-authenticated checkout snapshots; never install')
 args=parser.parse_args()
 if args.emit_dir is not None:
  if not args.prepare: parser.error('--emit-dir requires --prepare')
  if not args.emit_dir.is_absolute() or args.emit_dir.exists() or args.emit_dir.is_symlink(): parser.error('--emit-dir must be a new absolute directory')
  if any(p.is_symlink() for p in args.emit_dir.parents): parser.error('--emit-dir symlink ancestor')
  TARGET=args.emit_dir/TARGET.name
  TARGET_TEST=args.emit_dir/TARGET_TEST.name
 if not args.prepare and (not CAND.is_dir() or not REL.is_dir()): raise SystemExit('candidate_or_release_missing')
 module,source=source_module(snapshot=args.emit_dir is not None)
 test=authenticated_source_test(snapshot=args.emit_dir is not None)
 computed=None
 if not args.prepare:
  ch=frozen(module,CAND); rh=frozen(module,REL)
  if ch!=rh: raise SystemExit('candidate_release_not_twins')
  computed={
   'candidate':ch,'release':rh,
   'public':module.file_closure(REL/'public'),
   'functions':module.file_closure(REL/'functions'),
   'toolchain':module.toolchain_closure(module.TOOLCHAIN),
  }
  module.verify_immutable_tree(module.TOOLCHAIN,computed['toolchain'],toolchain=True)
  for path,pin in ((module.PYTHON,module.PYTHON_SHA256),(module.OPENSSL,module.OPENSSL_SHA256)):
   module.require_regular(path,root_owned=True,mode=0o755)
   if module.sha_file(path)!=pin: raise SystemExit(f'bootstrap_runtime_drift:{path}')
 repl={
  'Authority-bound one-time Build 6/6b plus Build 4 carrier (r4 / v1).':'Authority-bound one-time GLP34 carrier (r1 / v1).',
  'site-system/flagship-v2/candidates/glp-growth-build6-rebased-on-r13-build4-20260809-r4':'site-system/flagship-v2/candidates/glp34-provider-families-20260912',
  'site-system/flagship-v2/releases/glp-growth-build6-rebased-on-r13-build4-20260809-r4':'site-system/flagship-v2/releases/glp34-provider-families-20260918-r1',
  '/tmp/glp-growth-build6-r4-wrangler-workspace':'/tmp/glp34-r1-wrangler-workspace',
  'reports/glp-clinic-scout/growth/build-6/production-authority-20260809-r4':'reports/glp-clinic-scout/growth/glp34/production-authority-20260918-r1',
  'scripts/glp_deploy_build6_authority_v1.py':'scripts/glp_deploy_glp34_authority_v1.py',
  'tests/test_glp_deploy_build6_authority_v1.py':'tests/test_glp_deploy_glp34_authority_v1.py',
  'glp-build6-production-authority-v1':'glp34-production-authority-v1',
  'glp-build2-authority-v3-verify-receipt':'glp34-authority-v1-verify-receipt',
  'glp-build6-authority-v4-one-time-state':'glp34-authority-v1-one-time-state',
  'glp-build6-authority-v3-live-receipt':'glp34-authority-v1-live-receipt',
  'Build 6 r4 proportional authority carrier':'GLP34 r1 authority carrier',
 }
 for old,new in repl.items():
  if old not in source: raise SystemExit(f'missing_replacement:{old}')
  source=source.replace(old,new)
 repairs=[
  ('        if _mode(st) & 0o222:\n            fail(f"immutable_mode_drift:{path}:{_mode(st):04o}")\n    got = toolchain_closure(root) if toolchain else file_closure(root)\n','        if _mode(st) & 0o222:\n            fail(f"immutable_mode_drift:{path}:{_mode(st):04o}")\n        if not toolchain:\n            expected_mode = 0o555 if stat.S_ISDIR(st.st_mode) else 0o444\n            if _mode(st) != expected_mode:\n                fail(f"immutable_seal_mode_drift:{path}:{_mode(st):04o}")\n            if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:\n                fail(f"immutable_hardlink_drift:{path}:{st.st_nlink}")\n    got = toolchain_closure(root) if toolchain else file_closure(root)\n'),
  ('    if file_closure(RELEASE / "functions") != payload["release"]["functions"]:\n        fail("release_functions_closure_drift")\n    verify_immutable_tree(TOOLCHAIN, payload["toolchain"]["closure"], toolchain=True)\n','    if file_closure(RELEASE / "functions") != payload["release"]["functions"]:\n        fail("release_functions_closure_drift")\n    expected_routes = {\n        ("/", "public/index.html"),\n        ("/best/online-glp-1-providers/", "public/best/online-glp-1-providers/index.html"),\n        ("/cost/online-glp-1-programs/", "public/cost/online-glp-1-programs/index.html"),\n        ("/reviews/trt-nation/", "public/reviews/trt-nation/index.html"),\n        ("/quiz/provider-fit/", "public/quiz/provider-fit/index.html"),\n        ("/data/therapeutic-taxonomy.json", "public/data/therapeutic-taxonomy.json"),\n        ("/glp-1-price-index/", "public/glp-1-price-index/index.html"),\n    }\n    routes = payload.get("route_probes")\n    if not isinstance(routes, list) or len(routes) != len(expected_routes):\n        fail("authority_route_inventory_invalid")\n    observed_routes = set()\n    for item in routes:\n        if not isinstance(item, dict) or set(item) != {"url_path", "path", "sha256", "bytes"}:\n            fail("authority_route_spec_invalid")\n        pair = (item["url_path"], item["path"])\n        if not all(isinstance(value, str) for value in pair) or pair not in expected_routes:\n            fail("authority_route_path_invalid")\n        path = RELEASE / item["path"]\n        if type(item["bytes"]) is not int or item["bytes"] != path.stat().st_size or item["sha256"] != sha_file(path):\n            fail("authority_route_bytes_invalid")\n        observed_routes.add(pair)\n    if observed_routes != expected_routes:\n        fail("authority_route_inventory_invalid")\n    verify_immutable_tree(TOOLCHAIN, payload["toolchain"]["closure"], toolchain=True)\n'),
  ('def run_actual_preflight_sensor(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:\n    output = run_dir / "preflight-completeness.json"\n    if output.exists():\n        result = load_json(output)\n','def run_actual_preflight_sensor(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:\n    _attempt_id, run_dir = new_run(run_dir / "preflight-attempts", "preflight")\n    output = run_dir / "preflight-completeness.json"\n    if output.exists():\n        fail("preflight_sensor_output_collision")\n'),
  ('        result = load_json(output)\n    passed = [item.get("check") for item in result.get("checks", []) if item.get("status") == "PASS"]\n    skipped = [item.get("check") for item in result.get("checks", []) if item.get("status") == "SKIPPED"]\n    if result.get("status") != "PASS_PARTIAL" or result.get("failed_checks") != [] or result.get("failure_count") != 0:\n','        result = load_json(output)\n    checks = result.get("checks")\n    if (not isinstance(checks, list) or len(checks) != len(EXPECTED_SENSOR_PASS_CHECKS) + 1\n            or any(not isinstance(item, dict) or item.get("status") not in ("PASS", "SKIPPED") for item in checks)):\n        fail("preflight_sensor_raw_checks_invalid")\n    passed = [item.get("check") for item in checks if item.get("status") == "PASS"]\n    skipped = [item.get("check") for item in checks if item.get("status") == "SKIPPED"]\n    if (result.get("status") != "PASS_PARTIAL" or result.get("failed_checks") != []\n            or type(result.get("failure_count")) is not int or result.get("failure_count") != 0):\n'),
  ('    """Require the post-CAS sensor to close with PASS, zero skips, and zero failures."""\n    output = run_dir / "live-completeness.json"\n    if output.exists():\n        result = load_json(output)\n','    """Require the post-CAS sensor to close with PASS, zero skips, and zero failures."""\n    _attempt_id, run_dir = new_run(run_dir / "live-attempts", "live")\n    output = run_dir / "live-completeness.json"\n    if output.exists():\n        fail("live_sensor_output_collision")\n'),
  ('        result = load_json(output)\n    passed = [item.get("check") for item in result.get("checks", []) if item.get("status") == "PASS"]\n    skipped = [item.get("check") for item in result.get("checks", []) if item.get("status") == "SKIPPED"]\n    base_chain = next((item for item in result.get("checks", []) if item.get("check") == "base_chain"), {})\n    if result.get("status") != "PASS" or result.get("failed_checks") != [] or result.get("failure_count") != 0:\n','        result = load_json(output)\n    checks = result.get("checks")\n    if (not isinstance(checks, list) or len(checks) != len(EXPECTED_LIVE_SENSOR_PASS_CHECKS)\n            or any(not isinstance(item, dict) or item.get("status") != "PASS" for item in checks)):\n        fail("live_sensor_raw_checks_invalid")\n    passed = [item.get("check") for item in checks if item.get("status") == "PASS"]\n    skipped = [item.get("check") for item in checks if item.get("status") == "SKIPPED"]\n    base_chain = next((item for item in checks if item.get("check") == "base_chain"), {})\n    if (result.get("status") != "PASS" or result.get("failed_checks") != []\n            or type(result.get("failure_count")) is not int or result.get("failure_count") != 0):\n'),
  ('    if not isinstance(provider_reads, int) or provider_reads not in (1, 2):\n','    if type(provider_reads) is not int or provider_reads not in (1, 2):\n'),
  ('        [str(NODE), "--test", *[str(script) for script in scripts]], cwd=str(RELEASE),\n','        [str(NODE), "--test", "--test-reporter=tap", *[str(script) for script in scripts]], cwd=str(RELEASE),\n'),
  ('    if "# fail 0" not in proc.stdout or not re.search(r"# pass [1-9][0-9]*", proc.stdout):\n        fail("facet_binding_sensor_tap_nonpass")\n    return {"status": "PASS_FACET_BINDING_SENSOR", "stdout_sha256": sha_bytes(proc.stdout.encode()),\n            "stderr_sha256": sha_bytes(proc.stderr.encode()), "provider_mutations": 0}\n','    counts = {}\n    for key in ("tests", "pass", "fail", "cancelled", "skipped", "todo"):\n        values = re.findall(rf"^# {key} ([0-9]+)$", proc.stdout, re.MULTILINE)\n        if len(values) != 1:\n            fail(f"facet_binding_sensor_tap_summary_invalid:{key}")\n        counts[key] = int(values[0])\n    if (counts["tests"] <= 0 or counts["pass"] != counts["tests"]\n            or any(counts[key] != 0 for key in ("fail", "cancelled", "skipped", "todo"))):\n        fail("facet_binding_sensor_tap_nonpass")\n    return {"status": "PASS_FACET_BINDING_SENSOR", "stdout_sha256": sha_bytes(proc.stdout.encode()),\n            "stderr_sha256": sha_bytes(proc.stderr.encode()), "provider_mutations": 0,\n            "summary": counts}\n'),
  ('    successor = base64.b64decode(intent["successor_bytes_base64"], validate=True)\n    mode = int(intent["final_mode"], 8)\n','    successor = base64.b64decode(intent["successor_bytes_base64"], validate=True)\n    parsed = json.loads(successor)\n    if not isinstance(parsed, dict):\n        fail("pointer_successor_shape_invalid")\n    for key in ("release_root", "deployment_id", "manifest", "final_receipt"):\n        if key not in parsed:\n            fail(f"pointer_successor_missing:{key}")\n    temp_name = intent["temp_name"]\n    if not isinstance(temp_name, str) or temp_name in ("", ".", "..") or Path(temp_name).name != temp_name:\n        fail("pointer_temp_name_invalid")\n    mode = int(intent["final_mode"], 8)\n'),
  ('    final_st = _pointer_exact(pointer, successor, mode, uid, gid)\n    parsed = json.loads(successor)\n    for key in ("release_root", "deployment_id", "manifest", "final_receipt"):\n        if key not in parsed:\n            fail(f"pointer_successor_missing:{key}")\n    return {"status": "PASS_ATOMIC_POINTER_CAS", "sha256": sha_bytes(successor),\n','    final_st = _pointer_exact(pointer, successor, mode, uid, gid)\n    return {"status": "PASS_ATOMIC_POINTER_CAS", "sha256": sha_bytes(successor),\n'),
  ('    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:\n','    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, envelope: bool = False) -> Any:\n'),
  ('        return payload.get("result")\n\n    def project(self)','        return payload if envelope else payload.get("result")\n\n    def project(self)'),
  ('    def deployments(self) -> list[dict[str, Any]]:\n        result = self.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments")\n        return result if isinstance(result, list) else []\n','    def deployments(self) -> list[dict[str, Any]]:\n        deployments = []\n        seen = set()\n        page = 1\n        totals = None\n        while True:\n            response = self.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments?page={page}&per_page=100", envelope=True)\n            result = response.get("result")\n            info = response.get("result_info")\n            if not isinstance(result, list) or not isinstance(info, dict):\n                fail("deployment_list_shape_invalid")\n            pages, count = info.get("total_pages"), info.get("total_count")\n            if (type(pages) is not int or type(count) is not int or pages < 0 or count < 0\n                    or type(info.get("page")) is not int or info["page"] != page):\n                fail("deployment_list_pagination_invalid")\n            if totals is None:\n                totals = (pages, count)\n            if totals != (pages, count) or (pages == 0 and (page != 1 or count != 0 or result)):\n                fail("deployment_list_changed_during_read")\n            for item in result:\n                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)\n                        or not DEPLOYMENT_ID.fullmatch(item["id"]) or item["id"] in seen\n                        or item.get("environment") not in ("production", "preview")):\n                    fail("deployment_list_identity_invalid")\n                seen.add(item["id"])\n                deployments.append(item)\n            if page >= pages:\n                if len(deployments) != count:\n                    fail("deployment_list_incomplete")\n                return deployments\n            page += 1\n'),
  ('    if "preflight_sensor" not in stages:\n        stages["preflight_sensor"] = run_actual_preflight_sensor(run_dir, payload)\n        checkpoint(receipt_path, receipt)\n    if "facet_binding_sensor" not in stages:\n        stages["facet_binding_sensor"] = run_facet_binding_sensor()\n        checkpoint(receipt_path, receipt)\n','    stages["preflight_sensor"] = run_actual_preflight_sensor(run_dir, payload)\n    checkpoint(receipt_path, receipt)\n    stages["facet_binding_sensor"] = run_facet_binding_sensor()\n    checkpoint(receipt_path, receipt)\n'),
  ('    stages["workspace"] = str(workspace)\n    checkpoint(receipt_path, receipt)\n    # No provider object, credential read, provider request, or provider process exists above this line.\n','    stages["workspace"] = str(workspace)\n    checkpoint(receipt_path, receipt)\n    _canary_id, canary_dir = new_run(run_dir / "canary-attempts", "canary")\n    stages["atomic_cas_canary"] = run_cas_canary(canary_dir)\n    checkpoint(receipt_path, receipt)\n    # No provider object, credential read, provider request, or provider process exists above this line.\n'),
  ('    candidates = [item for item in deployments\n                  if deployment_commit(item) == commit_hash and successful_production(item)]\n','    candidates = [item for item in deployments\n                  if deployment_commit(item) == commit_hash and item.get("environment") == "production" and deployment_branch(item) == PRODUCTION_BRANCH]\n'),
  ('    reconciled_prior_provider_mutation = False\n    if current_id == predecessor_id:\n','    reconciled_prior_provider_mutation = False\n    prior_intent = stages.get("pages_deploy_intent")\n    if prior_intent is not None and (not isinstance(prior_intent, dict)\n            or prior_intent.get("predecessor_id") != predecessor_id\n            or prior_intent.get("commit_hash") != commit_hash):\n        fail("pages_deploy_intent_binding_invalid")\n    if current_id != predecessor_id and prior_intent is None:\n        fail("candidate_head_without_this_run_deploy_intent")\n    if current_id == predecessor_id:\n'),
  ('        if created_intent:\n            command = [str(NODE), str(WRANGLER), "pages", "deploy", str(workspace / "deploy-public"),\n','        if created_intent:\n            immediate_head = production_deployment(client.project())\n            if immediate_head.get("id") != predecessor_id or not successful_production(immediate_head):\n                fail("canonical_predecessor_changed_before_publish")\n            verify_signed_predecessor(payload["predecessor"]["pointer"])\n            if file_closure(workspace / "deploy-public") != payload["wrangler_expected"]["deploy_directory"]:\n                fail("deploy_directory_changed_before_publish")\n            command = [str(NODE), str(WRANGLER), "pages", "deploy", str(workspace / "deploy-public"),\n'),
  ('        candidates = [item for item in deployments\n                      if deployment_commit(item) == commit_hash and successful_production(item)]\n','        candidates = [item for item in deployments\n                      if deployment_commit(item) == commit_hash and item.get("environment") == "production" and deployment_branch(item) == PRODUCTION_BRANCH]\n'),
  ('        if len(matching) != 1:\n            fail("deployed_candidate_reconciliation_ambiguous")\n','        if len(candidates) != 1 or len(matching) != 1:\n            fail("deployed_candidate_reconciliation_ambiguous")\n'),
  ('            stages["pointer_intent"] = build_pointer_intent(payload, deployment_id, run_id)\n            checkpoint(receipt_path, receipt)\n        stages["pointer_cas"] = atomic_pointer_cas(stages["pointer_intent"], lock_fd, production=True)\n','            stages["pointer_intent"] = build_pointer_intent(payload, deployment_id, run_id)\n            checkpoint(receipt_path, receipt)\n        intent = stages["pointer_intent"]\n        if not isinstance(intent, dict) or not isinstance(intent.get("created_at_utc"), str) or not intent["created_at_utc"]:\n            fail("pointer_intent_shape_invalid")\n        expected_intent = build_pointer_intent(payload, deployment_id, run_id, now=intent["created_at_utc"])\n        if intent != expected_intent:\n            fail("pointer_intent_authority_binding_invalid")\n        stages["pointer_cas"] = atomic_pointer_cas(intent, lock_fd, production=True)\n'),
  ('        if "live_completeness_sensor" not in stages:\n            stages["live_completeness_sensor"] = run_live_completeness_sensor(run_dir, deployment_id)\n            checkpoint(receipt_path, receipt)\n        if "live_facet_binding_sensor" not in stages:\n            stages["live_facet_binding_sensor"] = run_facet_binding_sensor()\n            checkpoint(receipt_path, receipt)\n','        stages["live_completeness_sensor"] = run_live_completeness_sensor(run_dir, deployment_id)\n        checkpoint(receipt_path, receipt)\n        stages["live_facet_binding_sensor"] = run_facet_binding_sensor()\n        checkpoint(receipt_path, receipt)\n'),
  ('        stages["canonical_post_live_sensor"] = {"deployment_id": deployment_id}\n        checkpoint(receipt_path, receipt)\n    except Exception as exc:\n','        stages["canonical_post_live_sensor"] = {"deployment_id": deployment_id}\n        successor = base64.b64decode(intent["successor_bytes_base64"], validate=True)\n        pointer_st = _pointer_exact(POINTER, successor, int(intent["final_mode"], 8), int(intent["final_uid"]), int(intent["final_gid"]))\n        if (pointer_st.st_dev, pointer_st.st_ino) != (stages["pointer_cas"]["st_dev"], stages["pointer_cas"]["st_ino"]):\n            fail("pointer_changed_after_live_sensor")\n        stages["pointer_post_live_sensor"] = {"sha256": sha_bytes(successor), "st_dev": pointer_st.st_dev, "st_ino": pointer_st.st_ino}\n        checkpoint(receipt_path, receipt)\n    except Exception as exc:\n'),
  ('        client.rollback(predecessor_id)\n        restored = production_deployment(client.project())\n        if restored.get("id") != predecessor_id:\n            fail(f"rollback_canonical_reconciliation_failed:{restored.get(\'id\')}")\n        receipt["status"] = "BLOCKED_ROLLED_BACK"\n','        verify_signed_predecessor(payload["predecessor"]["pointer"])\n        client.rollback(predecessor_id)\n        restored = production_deployment(client.project())\n        if restored.get("id") != predecessor_id:\n            fail(f"rollback_canonical_reconciliation_failed:{restored.get(\'id\')}")\n        verify_signed_predecessor(payload["predecessor"]["pointer"])\n        receipt["status"] = "BLOCKED_ROLLED_BACK"\n'),
 ]
 for old,new in repairs:
  if source.count(old)!=1: raise SystemExit('p39_carrier_context_drift')
  source=source.replace(old,new,1)
 if 'scripts/glp_deploy_build6_authority_v1.py' not in test: raise SystemExit('missing_test_carrier_binding')
 for old,new in repl.items(): test=test.replace(old,new)
 # The sealed historical tests assumed reusable sensor files and urllib.
 # P39 requires fresh attempts; the current sensor uses requests. Keep test
 # effects inside intercepted subprocesses and fixture directories.
 def replace_test(start,end,replacement):
  if test.count(start)!=1 or test.count(end)!=1: raise SystemExit('p39_test_context_drift')
  return test[:test.index(start)]+replacement+test[test.index(end):]
 test=replace_test('    def test_actual_pinned_sensor_receipt_passes_all_release_local_checks(self):\n',
                   '    def test_live_sensor_requires_exact_deployment_pass_zero_and_provider_read_count(self):\n',
 '''    def preflight_fixture(self):
        return {"status": "PASS_PARTIAL", "failed_checks": [], "failure_count": 0,
                "skipped_checks": ["base_chain"], "effects": carrier.EXPECTED_ZERO_EFFECTS,
                "release_root": str(carrier.RELEASE),
                "checks": ([{"check": name, "status": "PASS"}
                            for name in carrier.EXPECTED_SENSOR_PASS_CHECKS]
                           + [{"check": "base_chain", "status": "SKIPPED"}])}

    def test_actual_pinned_sensor_receipt_passes_all_release_local_checks(self):
        with tempfile.TemporaryDirectory(prefix="glp-r13-sensor-test-") as temp:
            def sensor(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text(json.dumps(self.preflight_fixture()))
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(carrier.subprocess, "run", side_effect=sensor):
                result = carrier.run_actual_preflight_sensor(Path(temp), {})
        self.assertEqual(result["status"], "PASS_PARTIAL")
        self.assertEqual(result["passed_checks"], carrier.EXPECTED_SENSOR_PASS_CHECKS)
        self.assertEqual(result["skipped_checks"], ["base_chain"])
        self.assertEqual(result["effects"], carrier.EXPECTED_ZERO_EFFECTS)

    def test_sensor_failure_cannot_be_hidden_or_weakened(self):
        broken = self.preflight_fixture()
        broken["checks"][0]["status"] = "FAIL"
        broken["failed_checks"] = ["coverage"]
        broken["failure_count"] = 1
        with tempfile.TemporaryDirectory(prefix="glp-r13-sensor-fail-") as temp:
            def sensor(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text(json.dumps(broken))
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(carrier.subprocess, "run", side_effect=sensor):
                with self.assertRaises(carrier.GateError):
                    carrier.run_actual_preflight_sensor(Path(temp), {})

''')
 # A subprocess mock writes each deliberately constructed raw fixture to the
 # newly allocated attempt path; no live sensor or provider call is made.
 test=test.replace('            output.write_text(json.dumps(strict))\n            accepted = carrier.run_live_completeness_sensor(root, deployment_id)',
 '''            def sensor(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text(json.dumps(strict))
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(carrier.subprocess, "run", side_effect=sensor):
                accepted = carrier.run_live_completeness_sensor(root, deployment_id)''')
 test=test.replace('                (root / "live-completeness.json").write_text(json.dumps(variant))\n                with self.assertRaisesRegex(carrier.GateError, error):\n                    carrier.run_live_completeness_sensor(root, deployment_id)',
 '''                def sensor(command, **_kwargs):
                    Path(command[command.index("--output") + 1]).write_text(json.dumps(variant))
                    return mock.Mock(returncode=0, stdout="", stderr="")
                with mock.patch.object(carrier.subprocess, "run", side_effect=sensor):
                    with self.assertRaisesRegex(carrier.GateError, error):
                        carrier.run_live_completeness_sensor(root, deployment_id)''')
 test=replace_test('    def test_pinned_live_sensor_uses_stdlib_get_and_receipts_provider_reads(self):\n',
                   '    def test_exact_toolchain_has_no_symlink_special_or_writable_member(self):\n',
 '''    def test_pinned_live_sensor_requests_are_intercepted(self):
        sensor_spec = importlib.util.spec_from_file_location("glp_r3_sensor", carrier.SENSOR)
        sensor = importlib.util.module_from_spec(sensor_spec)
        assert sensor_spec.loader is not None
        sensor_spec.loader.exec_module(sensor)
        deployment_id = "11111111-1111-1111-1111-111111111111"
        response = mock.Mock()
        response.json.return_value = {"success": True, "result": {
            "canonical_deployment": {"id": deployment_id}}}
        session = mock.Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory(prefix="glp-r3-sensor-provider-") as temp:
            pointer = Path(temp) / "current-content-base.json"
            pointer.write_text(json.dumps({"deployment_id": deployment_id}))
            with (mock.patch.object(sensor, "CONTENT_BASE", pointer),
                  mock.patch.object(sensor, "validate_pointer_shape"),
                  mock.patch.object(sensor, "load_env_value", return_value="intercepted"),
                  mock.patch("requests.Session", return_value=session)):
                result = sensor.check_base_chain(False)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["base_deployment_id"], deployment_id)
        self.assertEqual(result["live_canonical_deployment_id"], deployment_id)
        self.assertEqual(session.get.call_count, 1)
        # Current sensor does not emit P39's required provider_reads field;
        # the carrier correctly refuses its live receipt until this is repaired.
        self.assertNotIn("provider_reads", result)

''')
 # Keep legacy mode bodies byte-identical; embed maintained package-only code.
 entry='if __name__ == "__main__":\n    raise SystemExit(main())'
 legacy='def main(argv: list[str] | None = None) -> int:'
 if source.count(entry)!=1 or source.count(legacy)!=1: raise SystemExit('package_dispatch_context_drift')
 extension=Path(__file__).with_name('glp34_package_only.inc.py').read_text()
 source=source.replace(legacy,legacy.replace('main(', 'legacy_main('),1)
 source=source.replace(entry,extension+'\n\n'+entry,1)
 source=source.replace('The only accepted modes are ``--verify-only`` and ``--live``.',
                       'Legacy modes are ``--verify-only`` and ``--live``.  The separate\n``--package-only`` mode packages explicitly hash-bound public bytes without\nrelease authority, signatures, or provider writes.')
 compile(source,str(TARGET),'exec')
 carrier_bytes,test_bytes=source.encode(),test.encode()
 if args.emit_dir is not None: args.emit_dir.mkdir(mode=0o700)
 # Reconcile both targets before writing either: partial prior output is safe only
 # if its bytes and custody match this exact authenticated lineage.
 present={p:existing(p,b,allow_staged=(p==TARGET_TEST)) for p,b in ((TARGET,carrier_bytes),(TARGET_TEST,test_bytes))}
 if not args.prepare and (frozen(module,CAND)!=ch or frozen(module,REL)!=rh): raise SystemExit('twins_changed_during_generation')
 for path,text in ((TARGET,source),(TARGET_TEST,test)):
  if present[path]=='staged': upgrade_staged_test(path,text)
  elif not present[path]: atomic(path,text)
 for path,raw in ((TARGET,carrier_bytes),(TARGET_TEST,test_bytes)):
  if not existing(path,raw): raise SystemExit(f'target_missing_after_generation:{path}')
 print({'status':'PASS_PREPARED' if args.prepare else 'PASS_GENERATED','computed_closures':computed,'candidate_release_tree_sha256':None if args.prepare else ch['tree_sha256'],'file_count':None if args.prepare else ch['file_count'],'carrier_sha256':hashlib.sha256(carrier_bytes).hexdigest(),'test_sha256':hashlib.sha256(test_bytes).hexdigest(),'created':[str(p) for p in (TARGET,TARGET_TEST) if not present[p]],'upgraded':[str(p) for p in (TARGET,TARGET_TEST) if present[p]=='staged']})
if __name__=='__main__': main()
