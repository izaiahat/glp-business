"""Exercise the reviewed cron wrapper without running providers or Sheet writers."""
from pathlib import Path
import subprocess
import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "ops/hermes-cron-scripts/glp_affiliate_approval_reconciler.sh"


@pytest.mark.parametrize("failed_child", ["invitation", "approval"])
def test_child_failure_is_redacted_actionable_and_never_success(tmp_path, failed_child):
    source = WRAPPER.read_text()
    # Replace only execution dependencies in the disposable wrapper copy.
    source = source.replace("cd /home/ubuntu/business", f"cd {tmp_path}")
    source = source.replace("/home/ubuntu/.hermes/state/glp-affiliate-approval-reconciler.lock", str(tmp_path / "lock"))
    source = source.replace("/usr/bin/python3.12", str(tmp_path / "fake-python"))
    child = tmp_path / "fake-python"
    child.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"scripts/affiliate_{'application_invitation_reconciler' if failed_child == 'invitation' else 'approval_auto_reconciler'}.py\" ]; then\n"
        "  printf 'PRIVATE MAIL CONTENT'\n"
        "  printf 'PRIVATE TOKEN' >&2\n"
        "  exit 23\n"
        "fi\n"
        "exit 0\n"
    )
    child.chmod(0o700)
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(source)
    result = subprocess.run(["bash", str(wrapper)], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 23
    assert f"{failed_child} child failed (exit 23)" in result.stdout
    assert "Inspect the private reconciler receipt" in result.stdout
    assert "PRIVATE" not in result.stdout + result.stderr
    assert "Status:** ok" not in result.stdout
