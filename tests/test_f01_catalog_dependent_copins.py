"""Exact GLP93 receiving closure; Git preimages, no runtime imports/effects."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PREIMAGE = '1cdd2bfbe5735c8817d38aaea66f47f9e88bfb38'
REVIEWED = 'a5b25a1da933a489abbef04f5a301ef8038efab5'
PACKAGED = '26ba0f161e5ca3136c2c37d8219c4a267ed304f0'  # Accepted PR498 head.
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
        # Immutable accepted PR498 co-pin history, not a freeze of future J7.
        # Keep exact historical bytes; current pin closure is checked separately.
        failures = []
        new_pz = sha(git_bytes(PACKAGED, PZ))
        manifests = {}
        for relative in MANIFESTS:
            before = git_bytes(PREIMAGE, relative)
            after = git_bytes(PACKAGED, relative)
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
            after = git_bytes(PACKAGED, relative)
            old, new = map(sha, manifests[manifest])
            self.assertEqual(before.count(old.encode()), 2)
            expected = before.replace(old.encode(), new.encode())
            if after != expected:
                failures.append(relative + ': not exact two manifest digest substitutions')
            else:
                self.assertEqual(after.replace(new.encode(), old.encode()), before)

        before = git_bytes(PREIMAGE, DAILY)
        after = git_bytes(PACKAGED, DAILY)
        match = re.search(rb"== '([a-f0-9]{64})'", before)
        assert match is not None
        old = match.group(1)
        new = sha(git_bytes(PACKAGED, BATCH)).encode()
        self.assertEqual(before.count(old), 1)
        if after != before.replace(old, new):
            failures.append(DAILY + ': not exact reviewed batch digest substitution')
        else:
            self.assertEqual(after.replace(new, old), before)

        # All four historical generations close against their accepted Git tree.
        for relative in ('ops/glp/reporting-v2/source-manifest.json',) + MANIFESTS:
            pins = json.loads(git_bytes(PACKAGED, relative))['files']
            for absolute, expected in pins.items():
                self.assertTrue(absolute.startswith(PREFIX))
                actual = sha(git_bytes(PACKAGED, absolute.removeprefix(PREFIX)))
                if actual != expected:
                    failures.append(relative + ': leaf mismatch ' + absolute)

        # The original reviewed catalog fix and its retained evidence remain exact.
        for relative in (PZ, BATCH, 'scripts/marketplace_adapters/digistore24.py',
                         'tests/test_j4_partnerize_partial_catalog.py',
                         'ops/glp/reporting-v2/source-manifest.json',
                         'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh'):
            self.assertEqual(git_bytes(PACKAGED, relative), git_bytes(REVIEWED, relative))
            self.assertEqual((ROOT / relative).read_bytes(), git_bytes(REVIEWED, relative))

        self.assertEqual(failures, [], '\n'.join(failures))

        # Immutable PR498 packaging evidence, not a freeze of future main.
        package = 'ops/rebuild-source-manifest.json'
        old_doc = json.loads(git_bytes(PREIMAGE, package))
        new_doc = json.loads(git_bytes(PACKAGED, package))
        integrated_main = 'ea0b247976eb136e33bc1fd8219654a8482362c9'
        main_advances = ('scripts/article_review_recovery.py',
                         'tests/test_article_review_recovery.py',
                         'tests/test_recovery_copied_authorship.py')
        for ancestor, descendant in ((PREIMAGE, integrated_main),
                                     (integrated_main, PACKAGED), (PACKAGED, 'HEAD')):
            subprocess.run(['git', '-C', str(ROOT), 'merge-base', '--is-ancestor',
                            ancestor, descendant], check=True)
        main_doc = json.loads(git_bytes(integrated_main, package))
        self.assertEqual(set(main_doc['files']), set(old_doc['files']))
        self.assertEqual({p for p in old_doc['files']
                          if main_doc['files'][p] != old_doc['files'][p]}, set(main_advances))
        for relative in main_advances:
            committed = git_bytes(integrated_main, relative)
            self.assertEqual(git_bytes(PACKAGED, relative), committed)
            self.assertEqual(main_doc['files'][relative]['sha256'], sha(committed))
            metadata = dict(main_doc['files'][relative])
            metadata['sha256'] = old_doc['files'][relative]['sha256']
            self.assertEqual(metadata, old_doc['files'][relative])
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
                self.assertEqual(new_doc['files'][relative], main_doc['files'][relative])
        for relative in CLOSURE + (added, runner):
            if new_doc['files'][relative]['sha256'] != sha(git_bytes(PACKAGED, relative)):
                failures.append('package stale: ' + relative)
        old_header = {k: v for k, v in old_doc.items() if k != 'files'}
        new_header = {k: v for k, v in new_doc.items() if k != 'files'}
        # The accepted historical resolver repaired the inherited tracked count.
        self.assertEqual(new_header.pop('tracked_count'), len(new_doc['files']) + 1)
        old_header.pop('tracked_count')
        self.assertEqual(old_header, new_header)
        restored = json.loads(git_bytes(PACKAGED, package))
        del restored['files'][added]
        for relative in CLOSURE + (runner,) + main_advances:
            restored['files'][relative]['sha256'] = old_doc['files'][relative]['sha256']
        restored['tracked_count'] = old_doc['tracked_count']
        self.assertEqual((json.dumps(restored, indent=2) + '\n').encode(), git_bytes(PREIMAGE, package))
        runner_before = git_bytes(PREIMAGE, runner)
        runner_after = git_bytes(PACKAGED, runner)
        line = b'suites+=(tests/test_f01_catalog_dependent_copins.py)\n'
        self.assertEqual(runner_after.count(line), 1)
        self.assertEqual(runner_after.replace(line, b''), runner_before)
        self.assertEqual(failures, [], '\n'.join(failures))
        self.assert_current_committed_package()

    def assert_current_committed_package(self):
        # Current authority is committed Git, never a matching workspace self-repin.
        package = 'ops/rebuild-source-manifest.json'
        committed_package = git_bytes('HEAD', package)
        document = json.loads(committed_package)
        tracked = set(subprocess.check_output(
            ['git', '-C', str(ROOT), 'ls-tree', '-r', '--name-only', 'HEAD'],
            text=True).splitlines())
        self.assertEqual(set(document['files']) | {package}, tracked)
        self.assertEqual(document['tracked_count'], len(tracked))
        for relative, record in document['files'].items():
            committed = git_bytes('HEAD', relative)
            self.assertEqual((ROOT / relative).read_bytes(), committed,
                             'workspace leaf differs from committed HEAD: ' + relative)
            self.assertEqual(record['sha256'], sha(committed),
                             'committed package stale: ' + relative)
        self.assertEqual((ROOT / package).read_bytes(), committed_package,
                         'workspace package differs from committed HEAD')
        # Legitimate J7 successors must close their CURRENT reporting leaf pins,
        # independently of immutable PR498 reverse-byte history above. Translate
        # absolute installed destinations to this isolated source root only.
        for relative in ('ops/glp/reporting-v2/source-manifest.json',) + MANIFESTS:
            pins = json.loads(git_bytes('HEAD', relative))['files']
            for absolute, expected in pins.items():
                self.assertTrue(absolute.startswith(PREFIX))
                leaf = absolute.removeprefix(PREFIX)
                self.assertEqual(expected, sha(git_bytes('HEAD', leaf)),
                                 'current reporting leaf pin stale: ' + relative + ': ' + leaf)

    def test_committed_unrelated_advance_preserves_historical_closure(self):
        # Real disposable Git successor, not a working-tree self-repin authority.
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'candidate'
            subprocess.run(['git', 'clone', '--quiet', '--shared', str(ROOT),
                            str(candidate)], check=True)
            relative = 'scripts/article_review_recovery.py'
            leaf = candidate / relative
            leaf.write_bytes(leaf.read_bytes() + b'\n# Offline unrelated successor fixture.\n')
            package = candidate / 'ops/rebuild-source-manifest.json'
            document = json.loads(package.read_bytes())
            document['files'][relative]['sha256'] = sha(leaf.read_bytes())
            package.write_text(json.dumps(document, indent=2) + '\n')
            subprocess.run(['git', '-C', str(candidate), 'add', relative,
                            'ops/rebuild-source-manifest.json'], check=True)
            subprocess.run(['git', '-C', str(candidate), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid', 'commit',
                            '--quiet', '-m', 'Offline committed unrelated advance'], check=True)
            with patch(__name__ + '.ROOT', candidate):
                self.test_exact_genuine_preimage_to_successor_closure()

    def test_unauthorized_workspace_leaf_and_matching_self_repin_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'candidate'
            subprocess.run(['git', 'clone', '--quiet', '--shared', str(ROOT),
                            str(candidate)], check=True)
            relative = 'scripts/article_review_recovery.py'
            leaf = candidate / relative
            leaf.write_bytes(leaf.read_bytes() + b'\n# Unauthorized workspace fixture.\n')
            package = candidate / 'ops/rebuild-source-manifest.json'
            document = json.loads(package.read_bytes())
            document['files'][relative]['sha256'] = sha(leaf.read_bytes())
            package.write_text(json.dumps(document, indent=2) + '\n')
            self.assertEqual(document['files'][relative]['sha256'], sha(leaf.read_bytes()))
            with patch(__name__ + '.ROOT', candidate):
                with self.assertRaisesRegex(AssertionError,
                                           'workspace leaf differs from committed HEAD: ' + relative):
                    self.test_exact_genuine_preimage_to_successor_closure()


if __name__ == '__main__':
    unittest.main()
