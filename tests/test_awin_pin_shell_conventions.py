"""The discovered shell name is the exact rewrite key, not a suffix stem."""
from pathlib import Path

import pytest

import awin_pin_release as release


@pytest.mark.parametrize(
    "key,quote,prefix",
    [
        ("expected_filter_sha256", "'", ""),
        ("FILTER_SHA256", '"', "readonly "),
        ("FILTER_SHA256", "", "readonly "),
    ],
    ids=["observed-lowercase-single", "legacy-uppercase-double", "legacy-unquoted"],
)
def test_discovered_shell_pin_rewrites_only_one_exact_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, quote: str, prefix: str,
):
    payload = tmp_path / "payload.py"
    payload.write_text("VALUE = 'reviewed fixture bytes'\n")
    wrapper = tmp_path / "wrapper.sh"
    stale = "0" * 64
    digest = release.sha_bytes(payload.read_bytes())
    assignment = f"{prefix}{key}={quote}{stale}{quote}"
    text = (
        "#!/usr/bin/env bash\n"
        f"cd {tmp_path}\n"
        f'readonly FILTER="{payload}"\n'
        f"{assignment}\n"
        'actual_filter="$(sha256sum payload.py | cut -d\' \' -f1)"\n'
        f'[[ "$actual_filter" == "${key}" ]] || exit 2\n'
        f"# {assignment}\n"
        f"printf '%s\\n' '{key}={stale}'\n"
        f"{key}_backup={quote}{stale}{quote}\n"
    )
    wrapper.write_text(text)
    monkeypatch.setattr(release.report, "PYTHON_PIN_SOURCES", ())
    monkeypatch.setattr(release.report, "HERMES_SCRIPTS", tmp_path)

    sites = release.discover_file_pin_sites()
    assert len(sites) == 1
    assert sites[0].source == wrapper
    assert sites[0].target == payload
    # Exercise discovery -> staging, not a hand-constructed rewrite key.
    staged, updates = release.build_staged({payload}, sites)
    expected = text.replace(assignment + "\n", assignment.replace(stale, digest) + "\n", 1)
    assert staged == {wrapper: expected.encode()}
    assert sites[0].key == key
    assert updates == [{"target": str(payload), "pin_source": str(wrapper),
                        "kind": "shell", "sha256": digest}]
    assert wrapper.read_text() == text  # Staging never writes even its fixture.

    # Case/suffix/prefix lookalikes, malformed hashes/quotes and ambiguous
    # duplicate assignments must refuse rather than widening the match.
    invalid = [
        (f"{key.swapcase()}='{stale}'\n", 0),
        (f"{key}_SHA256='{stale}'\n", 0),
        (f"other_{key}='{stale}'\n", 0),
        (f"# {assignment}\n", 0),
        (f"{key}='{stale[:-1]}'\n", 0),
        (f"{key}='{stale}0'\n", 0),
        (f"{key}='{stale}\"\n", 0),
        (f"{key}='{stale}\n", 0),
        (f"{key}={stale}\"\n", 0),
        (f"{assignment}\n{assignment}\n", 2),
    ]
    for candidate, count in invalid:
        with pytest.raises(RuntimeError) as failure:
            release._replace_shell_pin(candidate, sites[0], digest)
        assert str(failure.value) == f"shell_pin_match_count:{wrapper}:{key}:{count}"
