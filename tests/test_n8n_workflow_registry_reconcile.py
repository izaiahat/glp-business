"""K4 regression: the retired Affiliati release transport stays inactive in the registry,
and the cron wrapper pins the committed registry bytes (otherwise the sensor refuses)."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'ops/n8n-workflow-registry.json'
WRAPPER = ROOT / 'ops/hermes-cron-scripts/n8n_workflow_registry_sensor.py'


class RegistryReconcileTests(unittest.TestCase):
    def test_affiliati_release_inactive_and_wrapper_pins_registry(self):
        rows = {w['id']: w for w in json.loads(REGISTRY.read_text())['workflows']}
        self.assertIs(rows['ubpIUcu56h97S1Sf']['active'], False)  # GLP-335 cutover deactivated it
        self.assertIs(rows['uabEqV8bzEYanLVJ']['active'], False)  # article-carrier stays inactive
        digest = hashlib.sha256(REGISTRY.read_bytes()).hexdigest()
        self.assertIn("'ops/n8n-workflow-registry.json':'%s'" % digest, WRAPPER.read_text())


if __name__ == '__main__':
    unittest.main()
