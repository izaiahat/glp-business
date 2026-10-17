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
WORKER = 'scripts/glp_affiliate_wire_backlog_worker.py'
HISTORICAL_WORKER_HEAD = '440f03781ccb9e9e003144cf23a9e3442dd06ecd'
HISTORICAL_WORKER_SHA256 = '16cac080089a5b75da8ab3fba6d875bdd30a46bee545e0dd751dc97ed30e1108'
WORKER_SHA256 = 'eba4103d5909750c450f9ba432ed9549638f61b43b473390e04b89bf7b41c10c'
HISTORICAL_HEAD = 'b455e4a9d99b7d393c719204489a59cb65cfc293'
HISTORICAL_BUILDER = 'a58ac5ecae59727c0106590305188e3db47af78fb399b043004a56fdf3cf8432'
CURRENT_BINDINGS = {
    BUILDER: 'c5cef691ddaf5d5e80a9337ed2181a68b430e0808301707847d3efe98dc6b506',
    HELPER: '113ff2f339be09305a80d3e963e6f1f4395bf18218dec975f344ccd916ec72c8',
    'scripts/glp_best_terms_current_bindings.py': 'e0f1e5f6f49e1159e48c4e43ab1f7843b53a014f56b1d61c385a853ef53dd974',
}
WIRE_SCANNER_SHA256 = 'a5c20feed5c2a8395d598fde28363d4a034682093629c5b90c7ab22dc984fcd1'
APPROVAL_CHAIN_CAPTURE_ROOT = '/home/ubuntu/business/reports/glp-affiliate-auto-reconciler/maxbounty-approval-chain'
WIRE_SCANNER = 'scripts/glp_wire_in_ready_queue.py'
HISTORICAL_WIRE_HEAD = '8929d6ee4c5208657c4a917e6012b31789744033'
HISTORICAL_WIRE_SHA256 = 'f1c097bbdfcb2a64e9445d89f0896a020b7c3f2eea9dde0f983a5e39140190c1'


def historical_wire_scanner_bytes(root):
    """Authentic legacy co-pin test input, never an execution fallback."""
    path = Path(root) / WIRE_SCANNER
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != WIRE_SCANNER_SHA256:
        raise RuntimeError('current_wire_scanner_drift')
    body = subprocess.check_output(['git', '-C', str(root), 'show', HISTORICAL_WIRE_HEAD + ':' + WIRE_SCANNER])
    if hashlib.sha256(body).hexdigest() != HISTORICAL_WIRE_SHA256:
        raise RuntimeError('historical_wire_scanner_drift')
    return body


def validate_current_base(root):
    for name, expected in CURRENT_BINDINGS.items():
        path = Path(root) / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('current_base_dependency_drift:' + name)


def historical_dependency_bytes(root, name):
    """Fixture input, not a production fallback or permission to run old code."""
    validate_current_base(root)
    if name == WORKER:
        path = Path(root) / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != WORKER_SHA256:
            raise RuntimeError('current_worker_drift')
        body = subprocess.check_output(['git', '-C', str(root), 'show', HISTORICAL_WORKER_HEAD + ':' + name])
        if hashlib.sha256(body).hexdigest() != HISTORICAL_WORKER_SHA256:
            raise RuntimeError('historical_worker_drift')
        return body
    if name != BUILDER:
        return (Path(root) / name).read_bytes()
    body = subprocess.check_output(['git', '-C', str(root), 'show', HISTORICAL_HEAD + ':' + name])
    if hashlib.sha256(body).hexdigest() != HISTORICAL_BUILDER:
        raise RuntimeError('historical_builder_drift')
    return body


def current_carrier_bytes(historical_current_base):
    """Named current dependencies and the exact omitted approval-chain root.

    Historical GLP331 carrier bytes remain immutable. Fixture-only invocation
    still substitutes fixture captures and never adds a production root.
    """
    old = ("  ['/home/ubuntu/business/" + BUILDER + "']='" + HISTORICAL_BUILDER + "'\n").encode()
    new = ''.join("  ['/home/ubuntu/business/" + name + "']='" + digest + "'\n"
                  for name, digest in CURRENT_BINDINGS.items()).encode()
    if historical_current_base.count(old) != 1:
        raise RuntimeError('current_base_carrier_preimage_drift')
    current = historical_current_base.replace(old, new)
    old_pin = b"wire_scanner_expected_sha256='f1c097bbdfcb2a64e9445d89f0896a020b7c3f2eea9dde0f983a5e39140190c1'"
    new_pin = ("wire_scanner_expected_sha256='" + WIRE_SCANNER_SHA256 + "'").encode()
    old_roots = b'--captures /home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine --recursive-captures'
    new_roots = old_roots.replace(b' --recursive-captures',
                                 (' --additional-captures ' + APPROVAL_CHAIN_CAPTURE_ROOT + ' --recursive-captures').encode())
    for previous, replacement in ((old_pin, new_pin), (old_roots, new_roots)):
        if current.count(previous) != 1:
            raise RuntimeError('current_capture_carrier_preimage_drift')
        current = current.replace(previous, replacement)
    old_worker = ("  ['/home/ubuntu/business/" + WORKER + "']='" + HISTORICAL_WORKER_SHA256 + "'").encode()
    new_worker = ("  ['/home/ubuntu/business/" + WORKER + "']='" + WORKER_SHA256 + "'").encode()
    if current.count(old_worker) != 1:
        raise RuntimeError('current_worker_carrier_preimage_drift')
    current = current.replace(old_worker, new_worker)
    dry_args = b'  wire_args=(--values "$fixture_root/sheet-values.json" --captures "$fixture_root/captures" --recursive-captures --fail-on-stuck)\n'
    extra = (b'  if [[ -d "$fixture_root/additional-captures" ]]; then\n'
             b'    wire_args+=(--additional-captures "$fixture_root/additional-captures")\n'
             b'  fi\n')
    if current.count(dry_args) != 1:
        raise RuntimeError('current_dry_capture_preimage_drift')
    current = current.replace(dry_args, dry_args + extra)
    return current
