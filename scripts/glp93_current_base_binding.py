"""Exact GLP93 successor; never repin the historical GLP331 oracle/cohort.

Historical fixture replay uses its authentic predecessor bytes *only after*
checking today's complete builder/helper pair. Current execution uses the new
pair, also pinned by the maintained current-base shell carrier. No live writes.
"""
import hashlib
from pathlib import Path
import subprocess

BUILDER = 'scripts/build_glp_affiliate_wirein_incremental_v2.py'
HELPER = 'scripts/glp_review_editorial_projection.py'
HISTORICAL_HEAD = 'b455e4a9d99b7d393c719204489a59cb65cfc293'
HISTORICAL_BUILDER = 'a58ac5ecae59727c0106590305188e3db47af78fb399b043004a56fdf3cf8432'
CURRENT_BINDINGS = {
    BUILDER: 'cbe43edb08ca45b580a09508f3609139b1ddfaf699b32bd465a047f61ac8b9fd',
    HELPER: '6c9a7831568c9eaff82f6899381d49d2b512dfe2b5cc5e736525d4faf330ce33',
}


def validate_current_base(root):
    for name, expected in CURRENT_BINDINGS.items():
        path = Path(root) / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('current_base_dependency_drift:' + name)


def historical_dependency_bytes(root, name):
    """Fixture input, not a production fallback or permission to run old code."""
    validate_current_base(root)
    if name != BUILDER:
        return (Path(root) / name).read_bytes()
    body = subprocess.check_output(['git', '-C', str(root), 'show', HISTORICAL_HEAD + ':' + name])
    if hashlib.sha256(body).hexdigest() != HISTORICAL_BUILDER:
        raise RuntimeError('historical_builder_drift')
    return body


def current_carrier_bytes(historical_current_base):
    """Only the named builder substitution and new transitive helper pin."""
    old = ("  ['/home/ubuntu/business/" + BUILDER + "']='" + HISTORICAL_BUILDER + "'\n").encode()
    new = ''.join("  ['/home/ubuntu/business/" + name + "']='" + digest + "'\n"
                  for name, digest in CURRENT_BINDINGS.items()).encode()
    if historical_current_base.count(old) != 1:
        raise RuntimeError('current_base_carrier_preimage_drift')
    return historical_current_base.replace(old, new)
