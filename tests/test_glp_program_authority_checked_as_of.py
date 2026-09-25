#!/usr/bin/env python3
"""`checked_as_of` is required at PREPARE time, by name, per row.

WHY THIS EXISTS (2026-09-01, deploy-fable-20260901 — three rows lost, one batch
crashed).

`build_glp_affiliate_wirein_incremental_v2` subscripts `spec["checked_as_of"]`
twice — at :970 for `network_contract.local_evidence_checked_through` and at
:1027 for the provider row — and the value is rendered to readers as
"Checked <date>". It is not optional.

But nothing between the authoring engine and the shared build ever looked for
it:

  * `glp_onboarding_authoring_dispatch.AUTHORITY_PROGRAM_KEYS` (:358) — the key
    list the authoring brief hands the engine — does not name it, and the parity
    test that guards that list is one-directional (it proves every live
    authority has every key the CONTRACT names, never that the contract names
    every key the BUILDER requires), so once a key is missing from one authority
    on disk the intersection can only shrink;
  * `glp_onboarding_manifest.step_program_authority` (:1051) runs
    `wire_worker._validate_spec` precisely so that "a spec that would be refused
    at prepare is refused here" — and `_validate_spec` did not check it either.

So joi-blokes-compounded-glp-1s (xentraffic:3153), gatlan (3179) and
omzo-quiz-v2-scott-disick (3185) graded GREEN at authoring, passed prepare, and
then died as a bare `KeyError: 'checked_as_of'` INSIDE THE SHARED BUILD. A
shared-build crash is run-wide: one missing field in one authority is what
"shared_build_failure" says on nineteen other rows' block receipts.

What these tests pin:

  * a spec with no `checked_as_of` is refused BY NAME at prevalidation, so the
    row blocks with an actionable reason instead of crashing the batch;
  * a `checked_as_of` that is not an ISO calendar date is refused by name too,
    with the offending value in the reason (a `2026-9-1` or a `today` is a
    fabricated recency claim, not a date);
  * a valid date is inert — the validator moves straight on to the next check,
    so nothing that already carried the field changes behaviour;
  * and the CONTRACT itself: every key the builder subscripts off `spec` is
    still guarded here. That last one is the anti-regression bite — it is what
    stops the next builder-required field from going missing the same way.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

BUSINESS = Path(__file__).resolve().parents[1]
if not (BUSINESS / "scripts").is_dir():          # running from the receipts dir
    BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS / "scripts"))
sys.path.insert(0, str(BUSINESS / "ops"))

import glp_affiliate_wire_backlog_worker as worker  # noqa: E402

BUILDER = BUSINESS / "scripts/build_glp_affiliate_wirein_incremental_v2.py"


class CheckedAsOfIsRequiredAtPrepareTest(unittest.TestCase):
    """A fixed-host rail (Awin) is used deliberately: it keeps the fixture off
    the XenTraffic identity resolver and off live marketplace bytes, so this
    test asserts the validator's own rule and nothing else."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.terms = root / "awin-126727.json"
        self.terms.write_text("{}")
        self.research = root / "measured.json"
        self.research.write_text("{}")
        self.addCleanup(self._tmp.cleanup)

    def _candidate(self):
        return {"site_program_id": "measured", "sheet_row": 240,
                "provider_program_id": "126727", "network": "Awin"}

    def _spec(self, **overrides):
        spec = {
            "program_id": "measured",
            "sheet_row": 240,
            "advertiser_id": "126727",
            "network": "Awin",
            "terms_classification_path": str(self.terms),
            "research_file": self.research.name,
            "official_url": "https://trymeasured.com/",
            "checked_as_of": "2026-08-31",
        }
        spec.update(overrides)
        return spec

    def _validate(self, spec):
        worker._validate_spec(spec, self._candidate(), self.research, self.terms)

    def test_a_missing_checked_as_of_is_refused_by_name(self):
        spec = self._spec()
        spec.pop("checked_as_of")
        with self.assertRaises(worker.B2Error) as caught:
            self._validate(spec)
        self.assertEqual(str(caught.exception), "spec_checked_as_of_missing")

    def test_a_blank_checked_as_of_is_refused_by_the_same_name(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                with self.assertRaises(worker.B2Error) as caught:
                    self._validate(self._spec(checked_as_of=blank))
                self.assertEqual(str(caught.exception), "spec_checked_as_of_missing")

    def test_a_value_that_is_not_an_iso_calendar_date_is_refused_with_the_value(self):
        for bad in ("today", "2026-9-1", "31-08-2026", "2026-08-31T00:00:00Z",
                    "2026-13-01", "2026-02-30"):
            with self.subTest(bad=bad):
                with self.assertRaises(worker.B2Error) as caught:
                    self._validate(self._spec(checked_as_of=bad))
                self.assertEqual(str(caught.exception), f"spec_checked_as_of_invalid:{bad}")

    def test_a_valid_date_is_inert_and_the_validator_moves_on(self):
        """The next check in `_validate_spec` is the section check. Reaching it
        proves the new gate passed and that nothing before it moved."""
        with self.assertRaises(worker.B2Error) as caught:
            self._validate(self._spec())
        self.assertEqual(str(caught.exception), "spec_section_missing:page_copy")

    def test_no_new_authority_loses_the_field(self):
        """The known gap, named — so a NEW one is a failure and the old ones are
        a countdown rather than a silence.

        The three rows this defect cost on 2026-09-01, plus live-vital-launch
        which carries the identical gap outside that batch. The follow-up
        re-authoring run (deploy-remainder-20260901 item 3) empties this set;
        the test keeps passing when it does, and fails the moment any OTHER
        authority arrives without the field.
        """
        known_gap = {
            "gatlan",
            "joi-blokes-compounded-glp-1s",
            "omzo-quiz-v2-scott-disick",
            "live-vital-launch",
        }
        authority_dir = worker.PROGRAM_AUTHORITY_DIR
        if not authority_dir.is_dir():
            self.skipTest("no program-authorities directory in this tree")
        missing = set()
        for path in sorted(authority_dir.glob("*.json")):
            slug = path.stem
            try:
                spec = worker._extract_program_spec(worker.read_json(path), slug)
            except Exception:                     # noqa: BLE001 - not this test's subject
                continue
            if not str(spec.get("checked_as_of") or "").strip():
                missing.add(slug)
        self.assertEqual(
            sorted(missing - known_gap), [],
            "a program authority reached disk with no checked_as_of. The builder "
            "subscripts that field, so this row will crash the SHARED build, not "
            "block itself. Fix the authoring brief "
            "(glp_onboarding_authoring_dispatch.AUTHORITY_PROGRAM_KEYS), not this list.")


class TheBuilderContractIsStillGuardedTest(unittest.TestCase):
    """Anti-regression. `_validate_spec` exists so a spec the builder cannot
    render never reaches the SHARED build. Any key the builder subscripts off
    `spec` with `spec["..."]` must therefore be guarded here by name.

    This is the direction the authoring-dispatch parity test does not test.
    """

    #: Keys the builder subscripts that `_validate_spec` guards under a
    #: different refusal name. Each entry names the refusal, so adding one is a
    #: deliberate act with a citation rather than a silent widening.
    GUARDED_ELSEWHERE = {
        "program_id": "spec_identity_drift:program_id",
        "sheet_row": "spec_identity_drift:sheet_row",
        "advertiser_id": "spec_identity_drift:advertiser_id",
        "network": "spec_network_drift",
        "official_url": "spec_official_url_invalid",
        "research_file": "spec_research_file_drift",
        "terms_classification_path": "spec_terms_path_drift",
        "tracking_host": "spec_tracking_host_unresolvable",
        "provider_dynamic_profile": "spec_provider_dynamic_profile_missing",
        "page_copy": "spec_section_missing:page_copy",
        "row_contract": "spec_section_missing:row_contract",
        "field_sources": "spec_section_missing:field_sources",
        "checked_as_of": "spec_checked_as_of_missing",
    }

    #: FOUND BY THIS TEST, 2026-09-01. Two more keys the builder subscripts that
    #: `_validate_spec` still does not guard. Both are named in
    #: `glp_onboarding_authoring_dispatch.AUTHORITY_PROGRAM_KEYS`, so every
    #: authority on disk happens to carry them today — which is exactly what was
    #: true of `checked_as_of` until it was not. They are a latent repeat of the
    #: same defect and are listed, not hidden: the follow-up should add two more
    #: named refusals (`spec_display_name_missing`,
    #: `spec_logo_output_file_missing`) and empty this set.
    UNGUARDED_RESIDUAL_TRACKED = {
        "display_name": "no named refusal; KeyError inside the shared build",
        "logo_output_file": "no named refusal; KeyError inside the shared build",
    }

    def test_every_key_the_builder_subscripts_is_guarded_by_a_named_refusal(self):
        source = BUILDER.read_text()
        subscripted = set(re.findall(r'\bspec\["([a-z_]+)"\]', source))
        self.assertIn("checked_as_of", subscripted,
                      "the builder stopped requiring the field; delete this rule "
                      "deliberately rather than letting it rot")
        ungoverned = sorted(
            subscripted - set(self.GUARDED_ELSEWHERE) - set(self.UNGUARDED_RESIDUAL_TRACKED))
        self.assertEqual(
            ungoverned, [],
            "build_glp_affiliate_wirein_incremental_v2 hard-requires "
            f"{ungoverned} but _validate_spec does not refuse a spec that lacks "
            "them, so a missing one crashes the SHARED build instead of blocking "
            "one row — the 2026-09-01 defect, again")

    def test_the_worker_actually_carries_the_named_refusals(self):
        worker_source = Path(worker.__file__).read_text()
        for key, refusal in self.GUARDED_ELSEWHERE.items():
            name = refusal.split(":")[0]
            self.assertIn(f'"{name}', worker_source, f"{key} -> {refusal}")


if __name__ == "__main__":
    unittest.main()
