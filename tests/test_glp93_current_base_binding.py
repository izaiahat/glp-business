"""Current GLP93 inputs cannot inherit the historical GLP331 acceptance."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    path = ROOT / 'scripts/glp93_current_base_binding.py'
    assert path.is_file(), 'explicit current-base builder/helper binding is missing'
    spec = importlib.util.spec_from_file_location('current_binding', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('tampered', [None, 'scripts/build_glp_affiliate_wirein_incremental_v2.py', 'scripts/glp_review_editorial_projection.py'])
def test_current_base_exact_pair_and_tamper_refusal(tmp_path, tampered):
    module = load()
    for name in module.CURRENT_BINDINGS:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    module.validate_current_base(tmp_path)
    if tampered:
        with (tmp_path / tampered).open('ab') as handle:
            handle.write(b'\n# tampered\n')
        with pytest.raises(RuntimeError, match='current_base_dependency_drift'):
            module.validate_current_base(tmp_path)


def test_historical_builder_bytes_remain_exact_and_current_gate_runs_first(tmp_path):
    module = load()
    body = module.historical_dependency_bytes(ROOT, module.BUILDER)
    assert hashlib.sha256(body).hexdigest() == 'a58ac5ecae59727c0106590305188e3db47af78fb399b043004a56fdf3cf8432'
    expected = subprocess.check_output(['git', '-C', str(ROOT), 'show', 'b455e4a9d99b7d393c719204489a59cb65cfc293:'+module.BUILDER])
    assert body == expected
    for name in module.CURRENT_BINDINGS:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (tmp_path / module.BUILDER).write_bytes(body)
    with pytest.raises(RuntimeError, match='current_base_dependency_drift'):
        module.historical_dependency_bytes(tmp_path, module.BUILDER)
