"""Never send fixture operation receipts to the live billing ledger."""
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
import pytest

# Bind the regular checkout package before collection imports modules that add
# an installed BUSINESS root to sys.path. Namespace packages lose to an
# installed regular package even when the checkout root was first on sys.path.
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts'), str(ROOT / 'tests')]
import scripts
import tests
if Path(tests.__file__).resolve() != ROOT / 'tests/__init__.py':
    raise RuntimeError('pytest tests package is not the exact checkout source')
if Path(scripts.__file__).resolve() != ROOT / 'scripts/__init__.py':
    raise RuntimeError('pytest scripts package is not the exact checkout source')


@pytest.fixture
def j7_financial_clock(monkeypatch, request):
    """Keep portal closure, intake freshness and fake browser time coherent."""
    import affiliate_reporting_eden_portal_j7 as eden
    import affiliate_reporting_xentraffic_j7 as xentraffic

    class Clock(datetime):
        current = datetime(2026, 10, 12, 6, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    for module in (eden, xentraffic, request.module):
        monkeypatch.setattr(module, 'datetime', Clock)
    return Clock

SCRATCH = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/glp-tests')))
SCRATCH.mkdir(parents=True, exist_ok=True)
_LEDGER_DIR = tempfile.TemporaryDirectory(prefix='firecrawl-fixtures-', dir=SCRATCH)
os.environ.setdefault('FIRECRAWL_OPERATION_LEDGER', str(Path(_LEDGER_DIR.name)/'operations.jsonl'))


def pytest_unconfigure(config):
    _LEDGER_DIR.cleanup()
