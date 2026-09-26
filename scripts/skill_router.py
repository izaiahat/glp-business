#!/usr/bin/env python3
"""Resolve file surfaces to their owning EXPERT skills.

Why this exists (2026-09-15, operator): we built 49 expert skills for this
business and they were being used inconsistently, because routing was advisory
prose. `~/.hermes/skills/EXPERT-ROOTS.md` indexes expert tracks by noun but not
by PATH, so nothing could check whether a worker routed correctly for the files
it actually touched. This maps paths to owners so the check is mechanical.

Two consumers:
  * a worker, BEFORE starting: `skill_router.py --paths src/foo.mjs assets/...`
  * `result_lint.py`, AFTER: a RESULT that changed a mapped surface without
    declaring its owner in `skills_should_have_loaded` is rejected.

Deliberately partial. An unmapped path yields NO requirement rather than a
guess: a wrong required-skill trains people to ignore the check, which is worse
than no check. Grow `ops/skills/surface-router.json` as surfaces prove
themselves. A name must resolve to a real skill directory AND be a documented owner -- an
EXPERT-ROOTS track, or a CAPABILITY-MAP owner for a surface EXPERT-ROOTS does not list, which
is the fallback `.hermes.md` already prescribes. `linear-work-tracking` is the second kind and
its rules say so in their own `why`; the earlier absolute wording made the router contradict
the routing policy it implements. Never add a name that is neither.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

ROUTER = Path("/home/ubuntu/business/ops/skills/surface-router.json")
EXPERT_ROOTS = Path(os.path.expanduser("~/.hermes/skills/EXPERT-ROOTS.md"))


def load_rules(path: Path = ROUTER) -> list[dict]:
    try:
        return json.loads(path.read_text())["rules"]
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"skill_router: cannot read {path}: {exc}")


# --- which RESULT fields name the work's own output ------------------------------------
#
# The 2026-09-15 gate read `changed_files` ALONE, so a RESULT escaped the whole expert-skill
# requirement by naming the same file in `artifacts_created` (audit 2026-09-16). Unioning those
# two keys closes the reported case and leaves the CLASS open: the live corpus reports produced
# files under `deliverables` (167 RESULTs), `artifacts` (110), `outputs`, `files_created`,
# `artifacts_written`, `new_files`, `files_changed` and `modified_files` as well, and
# ops/linear/RESULT-SCHEMA.json sets additionalProperties:true, so the key space is open-ended.
# A hand-kept list of key names would rot the same way, so the rule is applied to the key NAME:
#
#   include  - the key names an OUTPUT NOUN (file/artifact/deliverable/output) AND is either
#              exactly that bare noun (`artifacts`, `deliverables`) or carries a PRODUCTION
#              token (`changed_files`, `artifacts_created`, `live_files_modified`).
#   exclude  - any token marks the key as an INPUT or an unchanged/derived fact
#              (`source_files_verified`, `protected_files_unchanged`, `input_artifacts`,
#              `artifact_count`). Routing on a file the work only READ is a FALSE requirement,
#              and a wrong required-skill trains people to ignore the check - the same reason
#              an unmapped path still requires nothing.
#
# Tokens are compared whole, so `unchanged` is never read as `changed`.
OUTPUT_NOUNS = {"file", "files", "artifact", "artifacts", "deliverable", "deliverables",
                "output", "outputs", "path", "paths", "surface", "surfaces"}
PRODUCTION_TOKENS = {"changed", "change", "changes", "modified", "modify", "modifications",
                     "created", "create", "creates", "written", "write", "writes", "wrote",
                     "added", "add", "additions", "new", "produced", "generated", "patched",
                     "patches", "edited", "edits", "updated", "delivered", "deleted", "deletions",
                     "removed", "touched", "authored", "emitted", "published"}
INPUT_TOKENS = {"unchanged", "verified", "verify", "read", "input", "inputs", "prior", "previous",
                "historical", "predecessor", "missing", "required", "needed", "known", "retained",
                "protected", "sealed", "held", "unrequested", "count", "counts", "sha256", "hash",
                "hashes", "status", "scope", "type", "root"}
# The schema's own two output arrays are in the gate unconditionally, whatever the rule above says.
ALWAYS_SURFACE_FIELDS = ("changed_files", "artifacts_created")


def _tokens(key: str) -> list[str]:
    """`ownedFiles` -> ['owned', 'files']; `files_created` -> ['files', 'created']."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


# Pass 2's adversarial verifier moved one mapped file out of `changed_files` into `ownedFiles`
# -- the tokenizer docstring's OWN worked example -- and the gate went silent. So did
# `output_files`, `deliverable_files`, `artifact_paths`, `affected_files`, `result_files`,
# `shipped_files`, `assessment_files` and `changed_files_verified`. 14 live RESULTs already name a
# mapped surface under such a key. The defect is structural, not a missing entry: an ALLOWLIST gate
# that must RECOGNISE a key name is a gate any novel key name walks past, and the key space is
# open-ended (RESULT-SCHEMA sets additionalProperties:true).
#
# So the default is inverted to fail CLOSED. Every key's path-shaped values are surfaces unless the
# key is affirmatively referential, and a PRODUCTION token overrides a referential one, which is what
# separates `changed_files_verified` (an output, verified) from `source_files_verified` (an input).
# A miss in REFERENCE_TOKENS now costs a visible false requirement, not a silent bypass.
NEGATION_TOKENS = {"denied", "refused", "blocked", "rejected", "failed", "skipped",
                   "unwritten", "uncreated", "unmodified", "attempted", "proposed", "planned"}
REFERENCE_TOKENS = INPUT_TOKENS | {
    "source", "sources", "baseline", "baselines", "evidence", "reference", "references",
    "backup", "backups", "parent", "pin", "pins", "receipt", "receipts", "authority",
    "command", "commands", "repro", "acceptance", "clause", "clauses", "skill", "skills",
    "dependency", "dependencies", "upstream", "existing", "current", "original", "pre",
    "consulted", "loaded", "expected", "candidate", "candidates", "note", "notes",
    "task", "job", "issue", "schema", "test", "tests", "log", "logs", "report", "reports",
}


def is_surface_field(key: str) -> bool:
    """True when this RESULT key names files the work produced or modified.

    Every one of the 14 verified bypasses carried an OUTPUT NOUN or a PRODUCTION token, so the
    defect was never the noun test -- it was the two extra conditions stacked on top of it:
    the `len(toks) == 1 or PRODUCTION` rule (which dropped `ownedFiles`, `output_files`,
    `artifact_paths`, `affected_files`, `result_files`, `shipped_files`, `assessment_files`) and a
    blanket referential veto (which dropped `changed_files_verified`). Both are gone. A PRODUCTION
    token now OVERRIDES a referential one, which is exactly what separates `changed_files_verified`
    (an output, verified) from `source_files_verified` (an input).
    """
    if key in ALWAYS_SURFACE_FIELDS:
        return True
    toks = _tokens(key)
    if not toks:
        return False
    # A negated production verb is not production: `candidate_writes_denied` names writes that
    # were REFUSED, so requiring their owning skill would be a false requirement.
    if any(t in NEGATION_TOKENS for t in toks):
        return False
    # Produced/modified wins outright: the key says the work wrote these.
    if any(t in PRODUCTION_TOKENS for t in toks):
        return True
    # Otherwise it must name a file-ish noun, and must not be marked referential.
    if not any(t in OUTPUT_NOUNS for t in toks):
        return False
    return not any(t in REFERENCE_TOKENS for t in toks)


def _looks_like_path(value) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    # One whitespace-free token containing a separator: a path, not a sentence that mentions one.
    return (bool(s) and "/" in s and len(s) <= 400 and len(s.split()) == 1
            and not s.lower().startswith(("http://", "https://")))


def _paths_in(value, depth: int = 0) -> list[str]:
    """Path-shaped strings inside a value: a list, a {path: meta} map, a [{'path': ...}] list."""
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value.strip()] if _looks_like_path(value) else []
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out += _paths_in(k, depth + 1) + _paths_in(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out += _paths_in(v, depth + 1)
    return out


def surfaces_of(doc) -> list[str]:
    """Every file path a RESULT says this work produced or modified, in first-seen order."""
    if not isinstance(doc, dict):
        return []
    seen: list[str] = []
    for key, value in doc.items():
        if not is_surface_field(key):
            continue
        for path in _paths_in(value):
            if path not in seen:
                seen.append(path)
    return seen


def required_for(paths, rules=None) -> dict:
    """Return {skill: [reasons]} for every mapped surface among `paths`."""
    rules = rules if rules is not None else load_rules()
    out: dict[str, list[str]] = {}
    for raw in paths:
        p = str(raw).strip()
        if not p:
            continue
        norm = p if p.startswith("/") else "/" + p.lstrip("./")
        for rule in rules:
            if fnmatch.fnmatch(norm, rule["glob"]) or fnmatch.fnmatch(p, rule["glob"]):
                for skill in rule["skills"]:
                    out.setdefault(skill, [])
                    reason = f"{Path(p).name}: {rule['why']}"
                    if reason not in out[skill]:
                        out[skill].append(reason)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve paths to owning expert skills.")
    ap.add_argument("--paths", nargs="*", default=[])
    ap.add_argument("--result", help="RESULT.json; reads every field naming files it produced or changed")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    paths = list(a.paths)
    if a.result:
        try:
            doc = json.loads(Path(a.result).read_text())
        except (OSError, ValueError) as exc:
            raise SystemExit(f"skill_router: cannot read {a.result}: {exc}")
        # The SAME field set the gate uses (surfaces_of), so a worker running this before the
        # work sees exactly what result_lint will require afterwards.
        paths += surfaces_of(doc)

    if not paths:
        print("skill_router: no paths given", file=sys.stderr)
        return 2

    req = required_for(paths)
    if a.json:
        print(json.dumps({"required_skills": sorted(req), "why": req}, indent=1))
        return 0
    if not req:
        print("No mapped surface among these paths. Declare skills_should_have_loaded_rationale "
              "explaining that no expert track owns them, rather than leaving it empty.")
        return 0
    print(f"Load these expert skills before the work ({EXPERT_ROOTS} is the current router):")
    for skill in sorted(req):
        print(f"  - {skill}")
        for reason in req[skill]:
            print(f"      {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
