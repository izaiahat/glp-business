"""Calendar CI must select the checked-out head for both supported events."""
from pathlib import Path
import os
import subprocess


def test_calendar_exact_head_guard_supports_pr_and_push():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/business-source-ci.yml").read_text()
    step = workflow.split("      - name: GLP-76 carrier calendar propagation", 1)[1].split("      - name:", 1)[0]
    expression = step.split("EXPECTED_HEAD: ${{ ", 1)[1].split(" }}", 1)[0]
    assert expression == "github.event.pull_request.head.sha || github.sha"
    guard = step.split("run: |\n", 1)[1].splitlines()[0].strip()
    assert guard == 'test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"'
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    for pr_head, push_head, expected_exit in [(head, "not-pr-head", 0), ("", head, 0), ("", "wrong-head", 1)]:
        selected = pr_head or push_head
        result = subprocess.run(["bash", "-c", guard], cwd=root, env={**os.environ, "EXPECTED_HEAD": selected})
        assert result.returncode == expected_exit
