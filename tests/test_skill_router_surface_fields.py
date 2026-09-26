"""The router gate must not be bypassable by renaming the key that holds the file list.

Pass 2's adversarial verifier moved one mapped surface out of `changed_files` into `ownedFiles` --
the tokenizer docstring's OWN worked example -- and the gate went silent; so did thirteen other
plausible key names, and 14 live RESULTs already used one. An ALLOWLIST gate that must recognise a
key name is a gate any novel key name walks past, and RESULT-SCHEMA sets additionalProperties:true.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import skill_router as router
import result_lint

# Every key the verifier proved evaded the gate while naming produced files.
VERIFIED_BYPASSES = [
    'ownedFiles', 'owned_files', 'output_files', 'deliverable_files', 'artifact_paths',
    'affected_files', 'result_files', 'shipped_files', 'output_artifacts', 'assessment_files',
    'changed_files_verified', 'artifacts_created_verified', 'changed_files_manifest',
    'deliverables_manifest',
]
# Keys that name inputs, unchanged facts, negated writes or unrelated scalars. Requiring a skill
# for these is a FALSE requirement, and a wrong required-skill trains people to ignore the check.
MUST_STAY_QUIET = [
    'source_files_verified', 'protected_files_unchanged', 'input_artifacts', 'artifact_count',
    'files_sha256', 'prior_artifacts', 'acceptance_clauses', 'skills_loaded',
    'skills_should_have_loaded', 'baseline_files', 'evidence_paths', 'backup_path',
    'candidate_writes_denied', 'writes_refused', 'files_skipped', 'job_id', 'issue', 'schema',
    'task_path', 'serial_reason', 'tests', 'cases', 'blockers',
]


def mapped_surface(rules):
    """A real path from the live router map, so this test cannot pass against a fictional rule."""
    for rule in rules:
        glob = rule.get('glob', '')
        if glob == '**/src/*quiz*' and rule.get('skills'):
            return 'site-system/flagship-v2/src/phase3-quiz-page.mjs', rule['skills'][0]
    raise AssertionError('expected the quiz rule in ops/skills/surface-router.json')


class SurfaceFieldTests(unittest.TestCase):
    def setUp(self):
        self.rules = router.load_rules()
        self.path, self.skill = mapped_surface(self.rules)

    def test_every_verified_bypass_key_is_now_a_surface_field(self):
        missed = [k for k in VERIFIED_BYPASSES if not router.is_surface_field(k)]
        self.assertEqual(missed, [], 'these key names still walk past the router gate')

    def test_reference_and_negated_keys_never_invent_a_requirement(self):
        fired = [k for k in MUST_STAY_QUIET if router.is_surface_field(k)]
        self.assertEqual(fired, [], 'these keys would create false skill requirements')

    def test_a_mapped_surface_is_found_under_every_bypass_key(self):
        for key in VERIFIED_BYPASSES:
            doc = {'changed_files': [], key: [self.path]}
            self.assertIn(self.path, router.surfaces_of(doc), key)
            self.assertIn(self.skill, router.required_for(router.surfaces_of(doc), self.rules), key)

    def test_result_lint_blocks_a_mapped_surface_hidden_under_a_renamed_key(self):
        for key in ('ownedFiles', 'output_files', 'artifact_paths', 'changed_files_verified'):
            with tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / 'RESULT.json'
                target.write_text(json.dumps({
                    'issue': 'GLP-999', 'status': 'COMPLETE', 'changed_files': [],
                    key: [self.path],
                    'skills_loaded': ['linear-work-tracking'],
                    'skills_should_have_loaded': ['linear-work-tracking'],
                }))
                problems = ' | '.join(str(p) for p in result_lint.lint(target))
                self.assertIn(self.skill, problems,
                              f'{key}: router gate did not fire for a mapped surface')

    def test_an_unmapped_path_still_requires_nothing_under_a_bypass_key(self):
        doc = {'ownedFiles': ['reports/ops/zzz/notes.txt']}
        self.assertEqual(router.required_for(router.surfaces_of(doc), self.rules), {})

    def test_negated_writes_are_not_surfaces_even_with_a_production_verb(self):
        doc = {'candidate_writes_denied': [self.path]}
        self.assertEqual(router.required_for(router.surfaces_of(doc), self.rules), {},
                         'a refused write is not a produced file')


class RouterMapIntegrityTests(unittest.TestCase):
    def test_every_skill_named_in_the_router_exists_on_disk(self):
        rules = router.load_rules()
        names = sorted({s for r in rules for s in r.get('skills', [])})
        self.assertTrue(names)
        missing = [n for n in names
                   if not (Path.home() / '.hermes/skills' / n / 'SKILL.md').exists()]
        self.assertEqual(missing, [], 'router names a skill with no SKILL.md')


if __name__ == '__main__':
    unittest.main()


class RouterOwnerProvenanceTests(unittest.TestCase):
    """Every routed skill must be a DOCUMENTED owner, not a name someone invented.

    `.hermes.md` routes to EXPERT-ROOTS first and falls back to CAPABILITY-MAP for surfaces
    EXPERT-ROOTS does not list, so either index is a legitimate provenance -- but neither being
    present means the router is naming an owner no document supports.
    """

    def test_every_routed_skill_is_documented_in_expert_roots_or_capability_map(self):
        roots = (Path.home() / '.hermes/skills/EXPERT-ROOTS.md').read_text()
        capmap = (Path.home() / '.hermes/skills/CAPABILITY-MAP.md').read_text()
        names = sorted({s for r in router.load_rules() for s in r.get('skills', [])})
        undocumented = [n for n in names if n not in roots and n not in capmap]
        self.assertEqual(undocumented, [], 'router names an owner no index documents')
