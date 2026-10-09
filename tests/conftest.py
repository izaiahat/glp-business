"""Never send fixture operation receipts to the live billing ledger."""
import os
from pathlib import Path
import tempfile

SCRATCH = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/glp-tests')))
SCRATCH.mkdir(parents=True, exist_ok=True)
_LEDGER_DIR = tempfile.TemporaryDirectory(prefix='firecrawl-fixtures-', dir=SCRATCH)
os.environ.setdefault('FIRECRAWL_OPERATION_LEDGER', str(Path(_LEDGER_DIR.name)/'operations.jsonl'))


def pytest_unconfigure(config):
    _LEDGER_DIR.cleanup()
