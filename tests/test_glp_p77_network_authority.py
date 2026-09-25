"""Unit-test the actual authority slices, not the worker's unreviewed entry point.

No worker import, deployment, tracking request, or business setup is executed.
This supplements, and does not replace, the registered integration suite.
"""

import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts/glp_affiliate_wire_backlog_worker.py"
RECEIPT = (
    "ops/linear/approvals/"
    "OPERATOR-DECISION-20260919-deploy-all-approved-no-terms-gate.json"
)
DECISION = "OPERATOR-DECISION-20260919-deploy-all-approved-no-terms-gate"
NETWORKS = ("xentraffic", "clickdealer")
LOGGER = "p77_authority_slice"


def load_authority_slice(business):
    tree = ast.parse(WORKER.read_text(encoding="utf-8"), filename=str(WORKER))
    names = {
        "GO_RUNTIME_PROMOTION_NETWORKS",
        "_go_runtime_network_authority",
        "_go_runtime_redirector_tos_authorized",
    }
    selected = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            key = node.name
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in names
        ):
            key = node.targets[0].id
        else:
            continue
        if key in selected:
            raise AssertionError(f"Duplicate authority definition: {key}")
        selected[key] = copy.deepcopy(node)
    if set(selected) != names:
        raise AssertionError("Missing patched authority definitions")

    producers = []
    consumers = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            for index, statement in enumerate(body[:-1]):
                if (
                    isinstance(statement, ast.Assign)
                    and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name)
                    and statement.targets[0].id == "network_authority"
                    and isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Name)
                    and statement.value.func.id == "_go_runtime_network_authority"
                ):
                    following = body[index + 1]
                    if not (
                        isinstance(following, ast.If)
                        and isinstance(following.test, ast.Name)
                        and following.test.id == "excluded_reason"
                    ):
                        raise AssertionError("Authority producer shape changed")
                    producers.append([statement, following])
        if isinstance(node, ast.If) and any(
            isinstance(part, ast.Name) and part.id == "live_authority"
            for part in ast.walk(node.test)
        ) and any(
            isinstance(part, ast.Constant)
            and part.value == "go_route_live_authority_contract_missing"
            for statement in node.body for part in ast.walk(statement)
        ):
            consumers.append(node.test)
    if len(producers) != 1 or len(consumers) != 1:
        raise AssertionError("Expected exactly one producer and consumer slice")

    producer = ast.parse(
        "def produce(network, excluded_reason=None):\n    return entry\n"
    ).body[0]
    producer.body = copy.deepcopy(producers[0]) + producer.body
    consumer = ast.parse(
        "def consume(live_authority):\n    return False\n"
    ).body[0]
    consumer.body[0].value = ast.UnaryOp(
        op=ast.Not(), operand=copy.deepcopy(consumers[0])
    )
    module = ast.Module(
        body=list(selected.values()) + [producer, consumer], type_ignores=[]
    )
    namespace = {"BUSINESS": business, "__name__": LOGGER}
    exec(compile(ast.fix_missing_locations(module), str(WORKER), "exec"), namespace)
    return namespace


class NetworkAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.business = Path(temporary.name)
        self.worker = load_authority_slice(self.business)
        self.produce = self.worker["produce"]
        self.consume = self.worker["consume"]
        self.config = self.worker["GO_RUNTIME_PROMOTION_NETWORKS"]
        self.receipt = self.business / RECEIPT
        self.receipt.parent.mkdir(parents=True)

    def write_receipt(self, **changes):
        document = {
            "schema": "operator-decision-v1",
            "decision_id": DECISION,
            "networks": list(NETWORKS),
        }
        document.update(changes)
        self.receipt.write_text(json.dumps(document), encoding="utf-8")

    def assert_withheld(self, entry, reason):
        self.assertEqual(entry, {
            "status": "WITHHELD",
            "reason": reason,
            "promotion_authorized": False,
            "constructor_verified": False,
            "redirector_tos_verified": False,
        })
        self.assertFalse(self.consume(entry))

    def accepted_entry(self, network="xentraffic"):
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            entry = self.produce(network)
        self.assertEqual(entry["status"], "VERIFIED")
        self.assertTrue(any(
            f"redirector_tos_unverified_for_network:{network}" in message
            and "basis=operator_risk_acceptance" in message
            and RECEIPT in message for message in logs.output
        ))
        return entry

    def test_url_networks_unchanged(self):
        for network in ("awin", "katalys", "affiliati"):
            with self.subTest(network=network):
                self.assertEqual(self.produce(network), {
                    "status": "VERIFIED",
                    "reason": "operator_authorized_live_go_runtime",
                    "network": network,
                    "promotion_authorized": True,
                    "constructor_verified": True,
                    "redirector_tos_verified": True,
                    **self.config[network],
                })
                self.assertTrue(self.consume(self.produce(network)))

    def test_receipt_networks_promote_truthfully(self):
        self.write_receipt()
        before = copy.deepcopy(self.config)
        for network in NETWORKS:
            with self.subTest(network=network):
                entry = self.accepted_entry(network)
                self.assertIs(entry.get("redirector_tos_verified"), True)
                self.assertIs(entry.get("risk_acceptance_logged"), True)
                self.assertEqual(entry.get("redirector_tos_basis"), "operator_risk_acceptance")
                self.assertEqual(entry.get("risk_acceptance_receipt"), RECEIPT)
                self.assertEqual(entry.get("redirector_tos_risk_acceptance"), RECEIPT)
                self.assertEqual(entry.get("redirector_tos_finding"),
                                 f"redirector_tos_unverified_for_network:{network}")
                self.assertNotIn("redirector_tos_source_url", entry)
                self.assertTrue(self.consume(entry))
        self.assertEqual(self.config, before)
        self.assertEqual(self.config["xentraffic"]["attribution_parameter_contract"], "xentraffic-sub1")
        self.assertEqual(self.config["clickdealer"]["attribution_parameter_contract"], "clickdealer-s1-s2")

    def test_network_without_authority_is_withheld(self):
        self.write_receipt(networks=["unlisted"])
        self.assert_withheld(self.produce("unlisted"),
                             "redirector_tos_unverified_for_network:unlisted")

    def test_config_without_either_source_is_withheld(self):
        self.config["unlisted"] = {"attribution_parameter_contract": "test-only"}
        self.assert_withheld(self.produce("unlisted"),
                             "redirector_tos_unverified_for_network:unlisted")

    def test_uncovered_receipt_is_withheld(self):
        self.write_receipt(networks=["clickdealer"])
        self.assert_withheld(self.produce("xentraffic"),
                             "redirector_tos_unverified_for_network:xentraffic")

    def test_receipt_is_read_again(self):
        self.write_receipt()
        entry = self.accepted_entry()
        self.write_receipt(networks=["clickdealer"])
        self.assert_withheld(self.produce("xentraffic"),
                             "redirector_tos_unverified_for_network:xentraffic")
        self.assertFalse(self.consume(entry))
        self.receipt.unlink()
        self.assertFalse(self.consume(entry))

    def test_invalid_receipts_are_withheld(self):
        self.assert_withheld(self.produce("xentraffic"),
                             "redirector_tos_unverified_for_network:xentraffic")
        invalid = [
            b"{", b"\xff", b"[]", b"null",
            {"schema": "wrong", "decision_id": DECISION, "networks": list(NETWORKS)},
            {"schema": "operator-decision-v1", "decision_id": "wrong", "networks": list(NETWORKS)},
            {"schema": "operator-decision-v1", "decision_id": DECISION},
            {"schema": "operator-decision-v1", "decision_id": DECISION, "networks": "xentraffic"},
            {"schema": "operator-decision-v1", "decision_id": DECISION, "networks": []},
        ]
        for document in invalid:
            with self.subTest(document=document):
                data = document if isinstance(document, bytes) else json.dumps(document).encode("utf-8")
                self.receipt.write_bytes(data)
                self.assert_withheld(self.produce("xentraffic"),
                                     "redirector_tos_unverified_for_network:xentraffic")

    def test_receipt_paths_are_business_relative(self):
        self.write_receipt()
        for path in (str(self.receipt), "../outside.json"):
            with self.subTest(path=path):
                self.config["xentraffic"]["redirector_tos_risk_acceptance"] = path
                self.assert_withheld(self.produce("xentraffic"),
                                     "redirector_tos_unverified_for_network:xentraffic")

    def test_explicit_hold_and_missing_network_survive(self):
        self.write_receipt()
        self.assert_withheld(self.produce("xentraffic", "option_c_provider_exclusion_preserved"),
                             "option_c_provider_exclusion_preserved")
        self.assert_withheld(self.produce(""), "runtime_network_authority_missing")

    def test_tampered_risk_payload_is_rejected(self):
        self.write_receipt()
        entry = self.accepted_entry()
        for field, value in (
            ("redirector_tos_basis", None),
            ("risk_acceptance_receipt", "other.json"),
            ("redirector_tos_risk_acceptance", "other.json"),
            ("risk_acceptance_logged", False),
            ("risk_acceptance_logged", 1),
            ("redirector_tos_finding", None),
            ("attribution_parameter_contract", "wrong"),
            ("network", "unlisted"),
            ("network", []),
            ("redirector_tos_source_url", "https://example.invalid/terms"),
        ):
            with self.subTest(field=field, value=value):
                self.assertFalse(self.consume({**entry, field: value}))

    def test_existing_consumer_guards_remain(self):
        self.write_receipt()
        entry = self.accepted_entry()
        self.assertFalse(self.consume(None))
        self.assertFalse(self.consume({**entry, "status": "WITHHELD"}))
        for field in ("promotion_authorized", "constructor_verified", "redirector_tos_verified"):
            for value in (False, 1, "true", None):
                with self.subTest(field=field, value=value):
                    self.assertFalse(self.consume({**entry, field: value}))

    def test_registered_receipt_covers_configured_networks(self):
        document = json.loads((ROOT / RECEIPT).read_text(encoding="utf-8"))
        self.assertEqual(document.get("schema"), "operator-decision-v1")
        self.assertEqual(document.get("decision_id"), DECISION)
        self.assertIsInstance(document.get("networks"), list)
        for network in NETWORKS:
            self.assertIn(network, document["networks"])
            self.assertEqual(self.config[network]["redirector_tos_risk_acceptance"], RECEIPT)


if __name__ == "__main__":
    unittest.main()
