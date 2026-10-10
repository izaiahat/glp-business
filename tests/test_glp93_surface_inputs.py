import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('unification', ROOT / 'scripts/glp_surface_unification.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)

class SurfaceInputTests(unittest.TestCase):
    def test_list_meds_keep_same_evidence_as_text(self):
        entry = {'insurance': 'Cash-pay only', 'meds_offered': ['Compounded injectable semaglutide', 'Compounded oral tirzepatide'], 'starting_price': 'Confirm selected plan'}
        text = dict(entry, meds_offered=' '.join(entry['meds_offered']))
        actual = u.evidence_flags(entry, 'fixture-review', {'calculator': {'mode': 'manual'}})
        self.assertEqual(actual, u.evidence_flags(text, 'fixture-review', {'calculator': {'mode': 'manual'}}))
        self.assertTrue(actual['multi_form'])
        self.assertFalse(actual['comparable_price'])

    def test_raw_direct_counts_include_all_three_new_rows(self):
        ids = ['old-' + str(i) for i in range(58)] + ['rightwell-review', 'willow-review', 'ivim-health-review']
        programs = [{'program_id': pid, 'commercial_state': 'live', 'profiles': {u.PROFILE: {'vertical_fit': True, 'wired': True, 'surfaces': {'review': True}}}} for pid in ids]
        ledger = {pid: {'live_link_allowed': True} for pid in ids}
        # Raw snapshot remains counted before the unchanged denial overlay.
        programs[0]['affiliate_relationship_snapshot'] = {'lifecycle': 'withdrawn'}
        self.assertEqual(u.raw_direct_live_count(programs, ledger), 61)
        ledger['rightwell-review']['live_link_allowed'] = 'true'
        self.assertEqual(u.raw_direct_live_count(programs, ledger), 60)
        ledger['rightwell-review']['promotion_authorized'] = True
        self.assertEqual(u.raw_direct_live_count(programs, ledger), 61)
        self.assertEqual(u.raw_direct_live_count(programs[:58], ledger), 58)

    def test_invalid_medication_values_refuse(self):
        for meds in [{'compounded': True}, ['oral', {'injectable': True}], 1]:
            with self.assertRaisesRegex(SystemExit, 'meds_offered_type_invalid'):
                u.evidence_flags({'meds_offered': meds}, 'fixture-review', {})

if __name__ == '__main__':
    unittest.main()
