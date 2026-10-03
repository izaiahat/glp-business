"""Offline checks for the retained launcher with only the packaged Python pin advanced."""
import hashlib
from pathlib import Path
import subprocess

SOURCE = Path(__file__).parents[1] / 'scripts/f13_atop_retention_launcher.c'
OLD = b'1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118'
NEW = b'e50d468e8b0adfb05733f5b87b3cff34829c4a8c1aea50c865aa8bdfe4bb150f'


def test_only_the_packaged_python_pin_differs_from_accepted_launcher():
    body = SOURCE.read_bytes()
    assert body.count(NEW) == 1 and OLD not in body
    predecessor = body.replace(NEW, OLD)
    assert hashlib.sha256(predecessor).hexdigest() == 'a0487b36f9e5f415beddd6c2cb1b3ec7308a92ce5fe2d72ab35a02b05ea3294a'
    assert b'child_argv[1] = (char *)"-I"' in body
    assert b'child_argv[2] = (char *)"-S"' in body


def test_static_launcher_build_and_no_mode_refusal(tmp_path):
    binary = tmp_path / 'launcher'
    subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static',
                    str(SOURCE), '-o', str(binary)], check=True)
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 126
    assert 'accepts exactly one mode' in result.stderr
    assert result.stdout == ''
