#!/usr/bin/env python3
"""Build GPT-6 Pro relay job packets (PROMPT.md + attachments) from a wave spec.

Registered producer for `reports/ops/gpt6pro-relay/jobs/<job>/{PROMPT.md,ATTACH-*.{json,md,txt},MANIFEST.json}`.
A packet is ONE message: a structured prompt (inline), small inputs inline, large inputs on Drive. The prompt asks
for complete, applyable deliverables (unified diffs, mutation scripts, runbook) so an Opus subagent can apply them
mechanically under the build lock and Fable reviews. Nothing here executes tests or touches the candidate.

Usage: gpt6pro_packet.py --wave ops/gpt6pro-relay/waves/<wave>.json [--job <name>] [--print-submit]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
JOBS = BUSINESS / "reports/ops/gpt6pro-relay/jobs"
MAX_CASES_PER_CAUSE = 4
MAX_CASE_CHARS = 9000

TEMPLATE = """# {title}

You are an independent senior verification engineer working for GLP Clinic Scout, a static comparison site
(Node build, self-hosted CSS/JS, Playwright browser harness). You have full web access: consult the CURRENT
Playwright, WCAG 2.2, axe-core and MDN documentation whenever an API, rule or threshold matters, and cite the URL
next to the claim. You may also create files. Work like a reviewer whose patch will be applied verbatim by a junior
engineer who will not fix mistakes: every diff must be complete and apply cleanly to the exact file contents given.

## Situation
Linear issue {issue}. Its acceptance, verbatim (the only authority for scope):
> {acceptance}

A classification run sorted the failing cases into causes. The causes assigned to you in this packet:
{cause_table}

The governing scope ruling is attached in full (`{ruling_name}`); the section that binds this packet:
{ruling_excerpt}

## Your task, per cause, in order
{task_items}

## Hard rules (a violation makes the whole answer unusable)
1. **Never weaken a detector.** No loosened tolerance, no `test.skip`, no `expect.soft` swap, no filtering of
   observed failures after the fact, no timeout increase as a fix. A repair makes the test exercise the REAL path.
2. **Mutation receipt for every repair.** For each cause give an exact, minimal mutation of the candidate (file,
   line, the changed text) that the REPAIRED test must FAIL on, plus the expected failure text. A repair you cannot
   mutation-test is not finished; say so.
3. **Cite only what is in this packet or on a URL you actually opened.** Line numbers refer to the exact file
   contents given here. Do not invent files, helpers, selectors or line numbers. If something needed is missing,
   name it under "Missing inputs" and proceed as far as the evidence allows.
4. **Diffs are complete.** Unified diff format, paths relative to the root named per file, full hunks with 3 lines
   of context, every touched file. Prefer small surgical hunks over rewrites. Keep code style identical to the file.
5. **UNKNOWN is a legitimate answer** when evidence does not decide. Never resolve by assumption in either direction.
6. Do not run anything on our behalf and do not assume a build happened. This is read-think-write.

## Inputs
Inline attachments follow this prompt (`===== ATTACHMENT: <name> =====`). Larger files are on Google Drive and are
listed at the end; open and read them fully before answering. Contents of the inline attachments:
{input_table}

## Deliverable format (Markdown, in this exact order)
1. `## Missing inputs` — bullet list or "none".
2. One `## <CAUSE_KEY>` section per cause, containing: **Diagnosis** (what the test/harness/candidate actually does
   vs what it must do, with `file:line` citations), **Verdict on the ruling** (agree / disagree and why, two
   sentences), **Patch** (fenced ```diff blocks), **Mutation receipt** (fenced block: the candidate mutation and the
   expected failing assertion text), **Doc citations** (URLs opened), **Risk** (what this repair could hide, one
   paragraph).
3. `## Runbook` — the exact shell commands, in order, to apply the patches from the harness root, run ONLY the
   affected tests, run each mutation and restore it. Assume `npx playwright test <file> -g "<title>"` style
   invocations; use the config files given.
4. `## Self-review` — you re-read your own diffs against the original file text and list every hunk whose context
   lines you verified character-for-character; any hunk you could not verify is marked UNVERIFIED.
5. A final fenced ```json block:
{json_schema}
6. **Files.** Also produce the patches as files: one `<CAUSE_KEY>.diff` per cause and `RUNBOOK.sh`. If your Google
   Drive connector can create files, save them into the folder "GLP Fable-GPT6Pro Relay" > "outbox" > "{job}" and
   list the file names you saved; if it cannot write, say `DRIVE_WRITE: unavailable` and make the files available
   as downloads instead. The inline diffs above remain the source of truth either way.

Be exhaustive on diffs and mutation receipts; be terse in prose. There is no length limit.
"""

TEMPLATE_FREEFORM = """# {title}

You are an independent senior engineer and reviewer working for GLP Clinic Scout, a static comparison site
(Node build from `src/` renderers into `public/`, self-hosted CSS/JS, node:test unit suite, Playwright browser
harness). You have full web access: consult the CURRENT documentation (Playwright, Node, WCAG 2.2, axe-core, MDN)
whenever an API, rule or threshold matters, and cite the URL next to the claim. You may create files. Work like a
reviewer whose output will be applied verbatim by a junior engineer who will not fix mistakes.

## Situation
Linear issue {issue}. Its acceptance, verbatim (the only authority for scope):
> {acceptance}

{situation}

## Binding rules for this packet
{ruling_excerpt}

## Your task, in order
{task_items}

## Hard rules (a violation makes the whole answer unusable)
1. **Never weaken a detector.** No loosened tolerance, no skipped test, no filtered output, no timeout increase as a
   fix, no edit to a pin or a test to make a check pass. Fix the producer, never its output.
2. **Cite only what is in this packet or on a URL you actually opened.** Line numbers refer to the exact file
   contents given here. Do not invent files, helpers, selectors, hashes or line numbers. If something needed is
   missing, name it under "Missing inputs" and proceed as far as the evidence allows.
3. **Any diff is complete**: unified format, paths relative to the root named per file, full hunks with 3 lines of
   context, every touched file, code style identical to the file. Every diff carries a mutation receipt (the change
   that must make the relevant test fail again) — a fix you cannot mutation-test is not finished; say so.
4. **UNKNOWN is a legitimate answer** when evidence does not decide. Never resolve by assumption in either direction.
5. Do not run anything on our behalf and do not assume a build happened. This is read-think-write.

## Inputs
Inline attachments follow this prompt (`===== ATTACHMENT: <name> =====`). Larger files are on Google Drive and are
listed at the end; open and read them fully before answering. Contents:
{input_table}

## Deliverable format (Markdown, in this exact order)
1. `## Missing inputs` — bullet list or "none".
2. One `## Task N` section per numbered task item above, answering it fully with `file:line` citations, fenced
   ```diff blocks where a change is proposed, and a fenced mutation-receipt block after every diff.
3. `## Runbook` — only if you proposed changes: the exact shell commands, in order, from the named root, to apply,
   prove, mutate and restore. Otherwise write "no changes proposed".
4. `## Self-review` — re-read your own diffs against the original file text and list every hunk whose context lines
   you verified character-for-character; mark any you could not verify UNVERIFIED. For an adjudication, list every
   claim you made and the attachment line or URL that supports it.
5. A final fenced ```json block:
{json_schema}
6. **Files.** If you produced diffs, also produce them as files (`<name>.diff`, `RUNBOOK.sh`). If your Google Drive
   connector can create files, save them into "GLP Fable-GPT6Pro Relay" > "outbox" > "{job}" and list the names;
   if it cannot write, say `DRIVE_WRITE: unavailable` and provide them as downloads. Inline content remains the
   source of truth either way.

Be exhaustive on evidence, diffs and receipts; be terse in prose. There is no length limit.
"""

TASK_ORACLE = """1. Read the cause's `reason`, its `source` line references, and every attached sample case (exact assertion
   text, evidence, traces). Then read the attached test/harness source in full.
2. Establish, from the code, what the test CURRENTLY exercises and why that differs from the behaviour the
   acceptance names. Quote the lines.
3. Decide: is the ruling's classification (oracle defect / environmental) correct for this cause? If you find the
   cause is actually a CANDIDATE defect, say so and stop at diagnosis for that cause (do not patch the test).
4. Write the repair: the test/harness change that makes it exercise the real path (keyboard focus for skip links,
   reaching the hidden state before asserting, binding an action to its actual effect, asserting a download, not
   attributing the harness's own `route.abort` / timeouts to the product, and so on per cause).
5. Write the mutation receipt: the candidate change that must make the repaired test fail.
6. State the expected effect on the case counts in this packet (which sample cases flip, which stay failed)."""

TASK_CANDIDATE = """1. Read each cause's `reason`, `source` references and every attached sample case (axe output, exact assertion
   text, emitted markup evidence). Then read the attached candidate source excerpts and the Drive files.
2. Locate the emitting code for each defect in the candidate SOURCE (`src/*.mjs` templates, CSS), not the built
   `public/` output: the fix must land in the producer so the next build emits it.
3. Consult the current WCAG 2.2 success criteria (2.5.8 target size minimum, 1.4.3/1.4.11 contrast) and the
   axe-core rule pages for each rule id in the evidence; cite them. Compute the numbers (target size, contrast
   ratios) from the evidence and show the arithmetic.
4. Write the fix as a unified diff against the given source. Keep design intent: fix the accessibility property
   with the smallest visual change, and say what changes visually.
5. Write the mutation receipt: with your fix applied, the assertion that now passes; and the single-line revert that
   makes it fail again.
6. Where the evidence does not identify the emitting code (e.g. a contrast ratio measured on a rendered state you
   cannot see), give the exact `grep`/inspection steps that will, and mark the cause UNKNOWN-PENDING-INSPECTION."""

JSON_SCHEMA = '''```json
{"issue": "%s", "job": "%s",
 "causes": {"<CAUSE_KEY>": {"classification_agrees_with_ruling": true, "verdict": "REPAIRED|CANDIDATE_DEFECT|UNKNOWN",
   "files_patched": ["<relative path>"], "mutation_receipt_present": true, "expected_cases_flipped": 0,
   "doc_urls": ["<url>"]}},
 "missing_inputs": ["..."], "drive_write": "saved|unavailable", "files_produced": ["..."]}
```'''


def repo_note(wave: dict) -> str:
    repo = wave.get("github_mirror")
    if not repo:
        return ""
    return (f"\n\n**Anything else you need is in the private GitHub repository `{repo}`** (connected to this account, "
            "read-only). Fetch files by path: `candidate/<path>` = the candidate tree (src/, tests/, scripts/, emitted "
            "public/ HTML, core assets), `harness/<path>` = the GLP-37 browser harness, `reports/` = governing reports. "
            "Use it whenever a diagnosis needs a file not attached here; cite fetched files as `repo:<path>:<line>`. "
            "Never treat repo contents as newer than the attachments for files that are attached.")


def load(p: Path) -> str:
    return p.read_text(errors="replace")


def excerpt(path: Path, a: int, b: int) -> str:
    lines = load(path).splitlines()
    a = max(1, a); b = min(len(lines), b)
    return "\n".join(f"{i:>5}| {lines[i-1]}" for i in range(a, b + 1))


def ruling_section(ruling: str, kind: str) -> str:
    # oracle-repair jobs get §1-3 (scope/repair/environmental); candidate-fix gets the discriminator + §2 preamble
    m = re.search(r"## The distinction this ruling turns on.*?(?=## Rulings)", ruling, re.S)
    head = m.group(0).strip() if m else ""
    if kind == "oracle-repair":
        m2 = re.search(r"### 2\. REPAIR THE TEST.*?(?=### 4\.)", ruling, re.S)
        body = m2.group(0).strip() if m2 else ""
    else:
        body = ("Candidate defects (C_*) are real product failures: they are fixed in the candidate SOURCE, never "
                "by excluding or loosening the test. C_CONTRAST is explicitly NOT excluded by the ruling.")
    return "\n".join("> " + l for l in (head + "\n\n" + body).splitlines())


ROOTS = {"harness": "harness_root", "candidate": "candidate_root", "business": None}


def packet_provenance(spec, wave):
    fields = ('dispatcher_profile', 'hermes_session_id', 'linear_child', 'request_id')
    data = {k: spec.get(k) or wave.get(k) for k in fields}
    if any(not isinstance(v, str) or not v.strip() for v in data.values()):
        raise SystemExit('PROVENANCE_REQUIRED: ' + ','.join(fields))
    return data


def build_freeform(spec: dict, wave: dict) -> Path:
    """kind=freeform: the spec carries the situation/task text and an explicit file list; no classification."""
    job = spec["job"]; d = JOBS / job; d.mkdir(parents=True, exist_ok=True)
    attach, drive_files = [], []
    n = 1
    for root, rel, desc, mode in spec["files"]:
        base = BUSINESS if ROOTS[root] is None else BUSINESS / wave[ROOTS[root]]
        src = base / rel
        if mode == 'unknown-test-sources':
            triage = json.loads(src.read_text())
            rows = [row for row in triage['tests'] if row.get('class') == 'UNKNOWN']
            if len(rows) != triage['denominator']['by_class']['UNKNOWN']:
                raise SystemExit('UNKNOWN_ROW_DENOMINATOR_MISMATCH ' + rel)
            for spec_path in sorted({row['spec'] for row in rows}):
                source = BUSINESS / wave['candidate_root'] / spec_path
                if not source.is_file():
                    raise SystemExit('UNKNOWN_TEST_SOURCE_MISSING ' + spec_path)
                p = d / ('DRIVE-SOURCE-' + spec_path.replace('/', '__'))
                p.write_bytes(source.read_bytes())
                drive_files.append((p, f"{desc} — `{spec_path}` (candidate source, full contents)"))
            n += 1
        elif mode == 'unknown-rows':
            data = json.loads(src.read_text())
            rows = [row for row in data['tests'] if row.get('class') == 'UNKNOWN']
            if len(rows) != data['denominator']['by_class']['UNKNOWN']:
                raise SystemExit('UNKNOWN_ROW_DENOMINATOR_MISMATCH ' + rel)
            p = d / f"DRIVE-UNKNOWN-{rel.replace('/', '__')}"
            p.write_text(json.dumps({'schema': data['schema'], 'source_report': data['source_report'],
                'source_binding': data['source_binding'], 'denominator': data['denominator'],
                'tests': rows}, indent=2) + '\n')
            drive_files.append((p, f"{desc} — `{rel}` (root: {root}); exact {len(rows)} UNKNOWN rows only")); n += 1
        elif mode.startswith("excerpt:"):
            a, b = [int(x) for x in mode.split(":")[1].split("-")]
            p = d / f"ATTACH-{n}-{root}-{rel.replace('/', '__')}.L{a}-{b}.txt"
            p.write_text(f"# {rel} lines {a}-{b} (root: {root}); line numbers are real file lines\n" + excerpt(src, a, b))
            attach.append((p, f"{desc} — `{rel}` lines {a}-{b} (root: {root})")); n += 1
        elif mode == "inline":
            p = d / f"ATTACH-{n}-{root}-{rel.replace('/', '__')}"; p.write_text(load(src)); attach.append((p, f"{desc} — `{rel}` (root: {root}), full contents")); n += 1
        else:
            p = d / ("DRIVE-" + rel.replace("/", "__")); p.write_text(load(src)); drive_files.append((p, f"{desc} — `{rel}` (root: {root}), FULL contents, on Drive"))
    input_table = "\n".join(f"- `{p.name}` — {desc}" for p, desc in attach) + ("\n" + "\n".join(f"- `{p.name}` — {desc}" for p, desc in drive_files) if drive_files else "") + repo_note(wave)
    prompt = TEMPLATE_FREEFORM.format(title=spec["title"], issue=wave["issue"], acceptance=wave["acceptance_verbatim"], situation=spec["situation"],
                             ruling_excerpt="\n".join("> " + l for l in spec.get("ruling", "").splitlines()) or "> (no separate ruling; the acceptance and the row text govern)",
                             task_items=spec["task"], input_table=input_table, json_schema=spec.get("json_schema", JSON_SCHEMA % (wave["issue"], job)), job=job)
    (d / "PROMPT.md").write_text(prompt)
    manifest = {**packet_provenance(spec, wave), "job": job, "job_class": spec["job_class"], "kind": "freeform", "causes": spec.get("rows", []),
                "attach": [str(p) for p, _ in attach], "drive_attach": [str(p) for p, _ in drive_files],
                "inline_chars": len(prompt) + sum(p.stat().st_size for p, _ in attach), "producer": "scripts/gpt6pro_packet.py", "wave": wave.get("_path")}
    (d / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    return d


def build_job(spec: dict, wave: dict, R: dict, ruling_text: str) -> Path:
    if spec.get("kind") == "freeform":
        return build_freeform(spec, wave)
    job = spec["job"]
    d = JOBS / job
    d.mkdir(parents=True, exist_ok=True)
    H = BUSINESS / wave["harness_root"]; C = BUSINESS / wave["candidate_root"]
    causes = {k: R["causes"][k] for k in spec["causes"]}
    # sample cases: whole objects only, never truncated mid-JSON
    per = {k: 0 for k in causes}; keep = []
    def slim(c):
        # keep the case whole except oversized trace/evidence blobs, so EVERY cause gets samples
        # (wave 1: E_LOCALHOST got zero samples because all its cases exceeded the size cap)
        c = dict(c)
        for key in ("traces", "evidence"):
            if key in c and len(json.dumps(c[key])) > 3000:
                c[key] = {"_trimmed": True, "head": json.dumps(c[key])[:3000]}
        return c
    for c in R["cases"]:
        ks = [k for k in c.get("causes", []) if k in causes]
        if not ks or not any(per[k] < MAX_CASES_PER_CAUSE for k in ks):
            continue
        s = json.dumps(c)
        if len(s) > MAX_CASE_CHARS:
            c = slim(c); s = json.dumps(c)
            if len(s) > MAX_CASE_CHARS * 2:
                continue
        for k in ks: per[k] += 1
        keep.append(c)
    missing = [k for k, n in per.items() if n == 0]
    if missing:
        print(f"  WARNING no sample cases for {missing}", file=sys.stderr)
    attach = []
    a1 = {"classification_source": R["source"], "classification_sha256": R["source_sha256"],
          "causes": causes, "sample_cases": keep,
          "note": f"{len(keep)} whole sample cases (max {MAX_CASES_PER_CAUSE} per cause, cases > {MAX_CASE_CHARS} chars omitted); "
                  f"case_count in each cause is the full count in the classification."}
    p = d / "ATTACH-1-causes-and-cases.json"; p.write_text(json.dumps(a1, indent=1)); attach.append((p, "cause entries + whole sample cases from the classification"))
    p = d / ("ATTACH-2-" + Path(wave["ruling"]).name); p.write_text(ruling_text); attach.append((p, "the full Fable scope ruling"))
    n = 3
    for rel in spec["harness_files"]:
        src = H / rel
        p = d / f"ATTACH-{n}-harness-{rel.replace('/', '__')}"; p.write_text(load(src)); attach.append((p, f"harness file `{rel}` (root: harness) — full contents")); n += 1
    for rel, a, b in spec.get("candidate_excerpts", []):
        p = d / f"ATTACH-{n}-candidate-{rel.replace('/', '__')}.L{a}-{b}.txt"
        p.write_text(f"# {rel} lines {a}-{b} (root: candidate); line numbers are real file lines\n" + excerpt(C / rel, a, b))
        attach.append((p, f"candidate excerpt `{rel}` lines {a}-{b}")); n += 1
    for rel, desc in spec.get("extra_business_files", []):
        src = BUSINESS / rel
        p = d / f"ATTACH-{n}-{Path(rel).name}"; p.write_text(load(src)); attach.append((p, desc)); n += 1
    drive_files = []
    routes = []
    for c in keep:
        r = c.get("route")
        if r and r not in routes: routes.append(r)
    for r in routes[:6]:
        src = C / "public" / r.strip("/") / "index.html"
        if src.exists():
            p = d / ("DRIVE-emitted-" + (r.strip("/").replace("/", "__") or "root") + ".html"); p.write_text(load(src))
            drive_files.append((p, f"EMITTED candidate page for route `{r}` (output of the build; never edit output) — on Drive"))
    for rel in spec.get("candidate_drive_files", []):
        src = C / rel
        p = d / ("DRIVE-" + rel.replace("/", "__")); p.write_text(load(src)); drive_files.append((p, f"candidate file `{rel}` (root: candidate) — FULL contents, on Drive"))
    cause_table = "\n".join(f"- **{k}** ({v['category']}, {v['case_count']} cases) — {v['title']}. Source: `{v.get('source')}`. Reason: {v.get('reason')}" for k, v in causes.items())
    input_table = "\n".join(f"- `{p.name}` — {desc}" for p, desc in attach) + ("\n" + "\n".join(f"- `{p.name}` — {desc}" for p, desc in drive_files) if drive_files else "") + repo_note(wave)
    title = f"{wave['issue']} — {'oracle/harness repair' if spec['kind'] == 'oracle-repair' else 'candidate defect fixes'}: {', '.join(causes)}"
    prompt = TEMPLATE.format(title=title, issue=wave["issue"], acceptance=wave["acceptance_verbatim"], cause_table=cause_table,
                             ruling_name=Path(wave["ruling"]).name, ruling_excerpt=ruling_section(ruling_text, spec["kind"]),
                             task_items=TASK_ORACLE if spec["kind"] == "oracle-repair" else TASK_CANDIDATE,
                             input_table=input_table, json_schema=JSON_SCHEMA % (wave["issue"], job), job=job)
    (d / "PROMPT.md").write_text(prompt)
    manifest = {**packet_provenance(spec, wave), "job": job, "job_class": spec["job_class"], "kind": spec["kind"], "causes": list(causes),
                "attach": [str(p) for p, _ in attach], "drive_attach": [str(p) for p, _ in drive_files],
                "inline_chars": len(prompt) + sum(p.stat().st_size for p, _ in attach),
                "producer": "scripts/gpt6pro_packet.py", "wave": wave.get("_path")}
    (d / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", required=True)
    ap.add_argument("--job")
    ap.add_argument("--print-submit", action="store_true")
    a = ap.parse_args()
    wave = json.loads(Path(a.wave).read_text()); wave["_path"] = a.wave
    R = json.loads((BUSINESS / wave["classification"]).read_text()) if wave.get("classification") else {}
    ruling_text = (BUSINESS / wave["ruling"]).read_text() if wave.get("ruling") else ""
    for spec in wave["jobs"]:
        if a.job and spec["job"] != a.job:
            continue
        d = build_job(spec, wave, R, ruling_text)
        m = json.loads((d / "MANIFEST.json").read_text())
        print(f"built {d.name}: inline_chars={m['inline_chars']:,} drive_files={len(m['drive_attach'])}")
        if a.print_submit:
            cmd = (f"python3 scripts/gpt6pro_relay.py submit --job {m['job']} --job-class {m['job_class']} --prompt {d}/PROMPT.md "
                   f"--attach {' '.join(m['attach'])}" + (f" --drive-attach {' '.join(m['drive_attach'])}" if m['drive_attach'] else ""))
            print("  " + cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
