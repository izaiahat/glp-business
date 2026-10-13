"""Collection must bind package imports to this checkout, not installed source."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_conftest_binds_scripts_before_installed_path_insertion(tmp_path):
    installed = tmp_path / 'installed'
    package = installed / 'scripts'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('INSTALLED_SOURCE = True\n')
    # Model the maintained modules that insert an installed BUSINESS root at
    # import time, after conftest but before the discovery/adoption suites.
    code = """
import json, runpy, sys
from pathlib import Path
root, installed = map(Path, sys.argv[1:])
sys.path[:0] = [str(root), str(root / 'scripts'), str(root / 'tests')]
runpy.run_path(str(root / 'tests/conftest.py'))
sys.path.insert(0, str(installed))
from scripts import discovery_predecessor_migration, glp44_scoped_adoption
print(json.dumps([str(Path(module.__file__).resolve()) for module in
                  (discovery_predecessor_migration, glp44_scoped_adoption)]))
"""
    env = {**os.environ, 'PYTHONPATH': str(installed), 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, '-c', code, str(ROOT), str(installed)],
                            cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        str(ROOT / 'scripts/discovery_predecessor_migration.py'),
        str(ROOT / 'scripts/glp44_scoped_adoption.py'),
    ]
