"""Pure confirmation-card contract from the curated router source."""
import ast
import re
from pathlib import Path
from typing import Any


def _card_function():
    source = Path(__file__).resolve().parents[1] / "ops/decisions_router.py"
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "confirmation_card")
    namespace = {"re": re, "Any": Any}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["confirmation_card"]


def test_long_natural_card_is_bounded_and_has_exact_receipt_identity():
    rows = [dict(item=i, status="already_satisfied_noop", text="Long operator item " + "x" * 90,
                 destination="inbox card handler", owner="inbox_card_handler", owner_outcome="terminal")
            for i in range(32)]
    card = _card_function()("live", rows, run_id="live-01ee148b60fac670e940")
    assert len(card) <= 1900
    assert "32 items" in card and "32 already satisfied/no-op" in card
    assert "reports/ops/decisions-router/runs/live-01ee148b60fac670e940.json" in card
    assert "Long operator item" not in card  # never imply omitted item detail is present


def test_small_card_keeps_per_item_disposition():
    card = _card_function()("live", [dict(item=1, status="applied_verified", text="exact item",
                                          destination="inbox/reconciler")], run_id="live-safe")
    assert "1. exact item" in card and "verified" in card
    assert "Full item dispositions" not in card
