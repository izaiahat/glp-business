#!/usr/bin/env python3
"""The wire-in gate's evidence-bound Link-QA value.

Default posture is unchanged and is asserted first: a wire-in declares ZERO
tracking clicks and writes `not_clicked_preserve_attribution`, because a
self-click pollutes attribution.

2026-08-18 added exactly one alternative. TRT Nation's AFF-TRT-001 hold could
only be cleared by verifying that a CJ deep link landed on the reviewed
weight-loss program instead of the provider's testosterone funnel, and that
cannot be established without observing the redirect. The bridge authorised the
QA. Writing `not_clicked_preserve_attribution` on a row we did click would have
put a false fact on the Sheet - which is the thing this writer exists to stop.

Every test below is a way to get `verified_redirect_to_official_200` onto the
Sheet without having earned it. All of them must be refused.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "affiliate_sheet_batch_writer", BUSINESS / "scripts/affiliate_sheet_batch_writer.py")
W = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(W)

DEEP_LINK = "https://www.tkqlhce.com/click-101822575-17115008?url=https%3A%2F%2Ftrtnation.com%2Fweight-loss-therapy%2F"


def qa_receipt(**overrides) -> dict:
    doc = {
        "schema": "glp.affiliate_link_click_qa.v1",
        "program_key": "CJ Affiliate:7538517",
        "link_click_qa_performed": True,
        "performed_at_utc": "2026-08-18T06:14:00Z",
        "tracking_url": DEEP_LINK,
        "observed_final_status": 200,
        "observed_final_url": "https://trtnation.com/weight-loss-therapy/?cjevent=abc123",
        "attribution": {"cjevent_param_present": True, "cjevent": "abc123"},
    }
    doc.update(overrides)
    return doc


class WireInLinkClickQa(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write_receipt(self, doc: dict) -> tuple[str, str]:
        path = Path(self.tmp.name) / "qa.json"
        raw = json.dumps(doc, sort_keys=True).encode("utf-8")
        path.write_bytes(raw)
        return str(path), hashlib.sha256(raw).hexdigest()

    def plan(self, *, qa: dict | None = None, link_qa: str, clicks: int, receipt=None) -> dict:
        entry: dict = {
            "row": 141,
            "cells": {"10": "approved_active_wired_live", "14": DEEP_LINK,
                      "19": "TRUE", "20": "TRUE", "21": link_qa, "24": "2026-08-18"},
        }
        if qa is not None:
            entry["link_click_qa"] = qa
        return {
            "schema": W.WIRE_IN_PLAN_SCHEMA,
            "authority_sha256": "a" * 64,
            "tracking_clicks": clicks,
            "rows": [entry],
        }

    # ---- the default posture is untouched ---------------------------------
    def test_the_ordinary_zero_click_wire_in_still_works_unchanged(self):
        plan = self.plan(link_qa="not_clicked_preserve_attribution", clicks=0)
        rows = W.wire_in_plan_rows(plan)
        self.assertEqual(rows[0]["row"], 141)

    def test_a_plan_with_no_receipts_may_not_declare_a_click(self):
        plan = self.plan(link_qa="not_clicked_preserve_attribution", clicks=1)
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_tracking_clicks_must_be_zero", str(ctx.exception))

    # ---- the receipted path ------------------------------------------------
    def test_a_real_receipt_earns_the_verified_value(self):
        path, sha = self.write_receipt(qa_receipt())
        plan = self.plan(qa={"receipt": path, "receipt_sha256": sha},
                         link_qa=W.WIRE_IN_QA_VERIFIED_LINK_QA, clicks=1)
        rows = W.wire_in_plan_rows(plan)
        self.assertEqual(rows[0]["cells"]["21"], "verified_redirect_to_official_200")

    def test_the_verified_value_without_any_receipt_is_refused(self):
        plan = self.plan(link_qa=W.WIRE_IN_QA_VERIFIED_LINK_QA, clicks=0)
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_link_qa_verified_without_receipt", str(ctx.exception))

    def test_a_row_that_did_click_may_not_claim_it_was_left_unclicked(self):
        # The inverse falsification, and the more tempting one: keep the safe
        # vocabulary while quietly having clicked.
        path, sha = self.write_receipt(qa_receipt())
        plan = self.plan(qa={"receipt": path, "receipt_sha256": sha},
                         link_qa="not_clicked_preserve_attribution", clicks=1)
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_link_qa_contradicts_receipt", str(ctx.exception))

    def test_an_invented_link_qa_value_is_still_refused(self):
        plan = self.plan(link_qa="looks_fine_to_me", clicks=0)
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_link_qa_not_allowed", str(ctx.exception))

    # ---- receipts that do not check out ------------------------------------
    def _refused(self, receipt_overrides: dict, expected: str, *, mutate_plan=None):
        path, sha = self.write_receipt(qa_receipt(**receipt_overrides))
        plan = self.plan(qa={"receipt": path, "receipt_sha256": sha},
                         link_qa=W.WIRE_IN_QA_VERIFIED_LINK_QA, clicks=1)
        if mutate_plan:
            mutate_plan(plan)
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn(expected, str(ctx.exception))

    def test_a_qa_that_was_never_performed_is_refused(self):
        self._refused({"link_click_qa_performed": False},
                      "wire_in_plan_link_click_qa_not_performed")

    def test_a_qa_that_never_reached_the_destination_is_refused(self):
        self._refused({"observed_final_status": 403},
                      "wire_in_plan_link_click_qa_status_not_200")

    def test_a_qa_with_no_attribution_stamp_is_refused(self):
        self._refused({"attribution": {"cjevent_param_present": True}},
                      "wire_in_plan_link_click_qa_attribution_missing")

    def test_a_stamp_that_is_not_on_the_final_url_is_refused(self):
        self._refused({"attribution": {"cjevent_param_present": True, "cjevent": "deadbeef"}},
                      "wire_in_plan_link_click_qa_attribution_missing")

    def test_a_receipt_about_a_different_link_is_refused(self):
        self._refused({"tracking_url": "https://www.tkqlhce.com/click-101822575-17100444"},
                      "wire_in_plan_link_click_qa_link_mismatch")

    def test_a_foreign_schema_is_refused(self):
        self._refused({"schema": "some.other.receipt.v1"},
                      "wire_in_plan_link_click_qa_schema_invalid")

    def test_a_receipt_whose_bytes_do_not_match_its_declared_hash_is_refused(self):
        path, _ = self.write_receipt(qa_receipt())
        plan = self.plan(qa={"receipt": path, "receipt_sha256": "0" * 64},
                         link_qa=W.WIRE_IN_QA_VERIFIED_LINK_QA, clicks=1)
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_link_click_qa_receipt_sha_mismatch", str(ctx.exception))

    def test_a_receipt_that_does_not_exist_on_disk_is_refused(self):
        plan = self.plan(qa={"receipt": "/nonexistent/qa.json", "receipt_sha256": "b" * 64},
                         link_qa=W.WIRE_IN_QA_VERIFIED_LINK_QA, clicks=1)
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_link_click_qa_receipt_missing", str(ctx.exception))

    def test_a_qa_block_with_no_binding_at_all_is_refused(self):
        plan = self.plan(qa={}, link_qa=W.WIRE_IN_QA_VERIFIED_LINK_QA, clicks=1)
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_link_click_qa_binding_invalid", str(ctx.exception))

    def test_one_receipt_cannot_authorise_two_declared_clicks(self):
        path, sha = self.write_receipt(qa_receipt())
        plan = self.plan(qa={"receipt": path, "receipt_sha256": sha},
                         link_qa=W.WIRE_IN_QA_VERIFIED_LINK_QA, clicks=2)
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_tracking_clicks_must_be_zero", str(ctx.exception))

    # ---- the mode's other rails are untouched ------------------------------
    def test_a_receipt_does_not_widen_the_status_set(self):
        path, sha = self.write_receipt(qa_receipt())
        plan = self.plan(qa={"receipt": path, "receipt_sha256": sha},
                         link_qa=W.WIRE_IN_QA_VERIFIED_LINK_QA, clicks=1)
        plan["rows"][0]["cells"]["10"] = "live_wired_verified"
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_status_not_allowed", str(ctx.exception))

    def test_a_receipt_does_not_widen_the_column_envelope(self):
        path, sha = self.write_receipt(qa_receipt())
        plan = self.plan(qa={"receipt": path, "receipt_sha256": sha},
                         link_qa=W.WIRE_IN_QA_VERIFIED_LINK_QA, clicks=1)
        plan["rows"][0]["cells"]["3"] = "rewritten program name"
        with self.assertRaises(RuntimeError) as ctx:
            W.wire_in_plan_rows(plan)
        self.assertIn("wire_in_plan_column_escape", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
