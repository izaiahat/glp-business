"""Exact GLP93 receiving closure; Git preimages, no runtime imports/effects."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREIMAGE = '1cdd2bfbe5735c8817d38aaea66f47f9e88bfb38'
REVIEWED = 'a5b25a1da933a489abbef04f5a301ef8038efab5'
PZ = 'scripts/marketplace_adapters/partnerize.py'
BATCH = 'scripts/affiliate_catalog_batch.py'
PREFIX = '/home/ubuntu/business/'
MANIFESTS = tuple('ops/glp/reporting-v2/source-manifest' + s + '.json'
                  for s in ('-j5', '-j6', '-j7'))
WRAPPERS = tuple('ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_' + s + '.sh'
                 for s in ('j5', 'j6', 'j7'))
DAILY = 'ops/hermes-cron-scripts/affiliate_supply_sweep_daily.sh'
CLOSURE = MANIFESTS + WRAPPERS + (DAILY,)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def git_bytes(commit, relative):
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', commit + ':' + relative])


class CatalogDependentClosureTests(unittest.TestCase):
    def test_exact_genuine_preimage_to_successor_closure(self):
        # No invented predecessor bytes or report-only proposals as authority.
        failures = []
        new_pz = sha((ROOT / PZ).read_bytes())
        manifests = {}
        for relative in MANIFESTS:
            before = git_bytes(PREIMAGE, relative)
            after = (ROOT / relative).read_bytes()
            old = json.loads(before)['files'][PREFIX + PZ]
            expected = before.replace(old.encode(), new_pz.encode())
            if after != expected:
                failures.append(relative + ': not exact Partnerize digest substitution')
            else:
                self.assertEqual(after.replace(new_pz.encode(), old.encode()), before)
            self.assertEqual(before.count(old.encode()), 1)
            old_doc, new_doc = json.loads(before), json.loads(after)
            self.assertEqual(old_doc.keys(), new_doc.keys())
            self.assertEqual(set(old_doc['files']), set(new_doc['files']))
            self.assertEqual({k: v for k, v in old_doc['files'].items() if k != PREFIX + PZ},
                             {k: v for k, v in new_doc['files'].items() if k != PREFIX + PZ})
            manifests[relative] = (before, expected)

        for relative, manifest in zip(WRAPPERS, MANIFESTS):
            before = git_bytes(PREIMAGE, relative)
            after = (ROOT / relative).read_bytes()
            old, new = map(sha, manifests[manifest])
            self.assertEqual(before.count(old.encode()), 2)
            expected = before.replace(old.encode(), new.encode())
            if after != expected:
                failures.append(relative + ': not exact two manifest digest substitutions')
            else:
                self.assertEqual(after.replace(new.encode(), old.encode()), before)

        before = git_bytes(PREIMAGE, DAILY)
        after = (ROOT / DAILY).read_bytes()
        match = re.search(rb"== '([a-f0-9]{64})'", before)
        assert match is not None
        old = match.group(1)
        new = sha((ROOT / BATCH).read_bytes()).encode()
        self.assertEqual(before.count(old), 1)
        if after != before.replace(old, new):
            failures.append(DAILY + ': not exact reviewed batch digest substitution')
        else:
            self.assertEqual(after.replace(new, old), before)

        # All four generations close against candidate bytes, not installed paths.
        for relative in ('ops/glp/reporting-v2/source-manifest.json',) + MANIFESTS:
            pins = json.loads((ROOT / relative).read_bytes())['files']
            for absolute, expected in pins.items():
                self.assertTrue(absolute.startswith(PREFIX))
                actual = sha((ROOT / absolute.removeprefix(PREFIX)).read_bytes())
                if actual != expected:
                    failures.append(relative + ': leaf mismatch ' + absolute)

        # The original reviewed catalog fix and its retained evidence remain exact.
        for relative in (PZ, BATCH, 'scripts/marketplace_adapters/digistore24.py',
                         'tests/test_j4_partnerize_partial_catalog.py',
                         'ops/glp/reporting-v2/source-manifest.json',
                         'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh'):
            self.assertEqual((ROOT / relative).read_bytes(), git_bytes(REVIEWED, relative))

        self.assertEqual(failures, [], '\n'.join(failures))

        # Packaging may update these seven entries and the offline test runner only.
        package = 'ops/rebuild-source-manifest.json'
        old_doc = json.loads(git_bytes(PREIMAGE, package))
        new_doc = json.loads((ROOT / package).read_bytes())
        added = 'tests/test_f01_catalog_dependent_copins.py'
        runner = 'scripts/run_tests.sh'
        self.assertEqual(set(new_doc['files']) - set(old_doc['files']), {added})
        self.assertEqual(set(old_doc['files']) - set(new_doc['files']), set())
        for relative, record in old_doc['files'].items():
            if relative in CLOSURE + (runner,):
                successor = dict(new_doc['files'][relative])
                successor['sha256'] = record['sha256']
                self.assertEqual(successor, record)
            else:
                self.assertEqual(new_doc['files'][relative], record)
        for relative in CLOSURE + (added, runner):
            if new_doc['files'][relative]['sha256'] != sha((ROOT / relative).read_bytes()):
                failures.append('package stale: ' + relative)
        old_header = {k: v for k, v in old_doc.items() if k != 'files'}
        new_header = {k: v for k, v in new_doc.items() if k != 'files'}
        # Current main declares 1121 while its genuine tracked tree has 1122.
        # The maintained resolver repairs this pre-existing count, not any source pin.
        self.assertEqual(new_header.pop('tracked_count'), len(new_doc['files']) + 1)
        old_header.pop('tracked_count')
        self.assertEqual(old_header, new_header)
        restored = json.loads((ROOT / package).read_bytes())
        del restored['files'][added]
        for relative in CLOSURE + (runner,):
            restored['files'][relative]['sha256'] = old_doc['files'][relative]['sha256']
        restored['tracked_count'] = old_doc['tracked_count']
        self.assertEqual((json.dumps(restored, indent=2) + '\n').encode(), git_bytes(PREIMAGE, package))
        runner_before = git_bytes(PREIMAGE, runner)
        runner_after = (ROOT / runner).read_bytes()
        line = b'suites+=(tests/test_f01_catalog_dependent_copins.py)\n'
        self.assertEqual(runner_after.count(line), 1)
        self.assertEqual(runner_after.replace(line, b''), runner_before)
        self.assertEqual(failures, [], '\n'.join(failures))


if __name__ == '__main__':
    unittest.main()
