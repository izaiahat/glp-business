"""Synthetic ownership controls; no host effects or external API calls."""
import base64
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import time
import unittest
from unittest.mock import patch
import tempfile
import os
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import host_disk_completed_dependencies as dep


class API:
    active = False
    removed = []
    def path_is_active_privileged(self, p):
        return self.active
    def remove_disposable(self, p, deadline):
        self.removed.append(str(p))
        shutil.rmtree(p)
        return True


class CompletedDependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.owner = self.base/'worktree'; self.owner.mkdir()
        self.root = self.owner/'node_modules'; self.root.mkdir()
        self.cache = self.base/'cache'; self.cache.mkdir()
        raw = io.BytesIO()
        self.files = {'package.json': b'{"name":"vendor","version":"1.0.0","bin":{"vendor":"cli.js"}}',
                      'credentials.js': b'export class Credentials {}\n',
                      'transactions.js': b'export function transaction() {}\n',
                      'cli.js': b'#!/usr/bin/env node\n'}
        with tarfile.open(fileobj=raw, mode='w:gz') as tf:
            for name, data in self.files.items():
                info = tarfile.TarInfo('package/'+name); info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        blob = raw.getvalue(); digest = hashlib.sha512(blob).digest()
        hexd = digest.hex(); c = self.cache/hexd[:2]/hexd[2:4]/hexd[4:]
        c.parent.mkdir(parents=True); c.write_bytes(blob)
        self.package = {'version':'1.0.0', 'resolved':'https://registry.npmjs.org/vendor/-/vendor-1.0.0.tgz',
                        'integrity':'sha512-'+base64.b64encode(digest).decode()}
        (self.root/'vendor').mkdir()
        for name, data in self.files.items(): (self.root/'vendor'/name).write_bytes(data)
        (self.root/'.bin').mkdir(); (self.root/'.bin/vendor').symlink_to('../vendor/cli.js')
        lock={'lockfileVersion':3,'packages':{'':{},'node_modules/vendor':self.package}}
        (self.owner/'package-lock.json').write_text(json.dumps(lock))
        (self.owner/'package.json').write_text('{"name":"owner"}')
        (self.root/'.package-lock.json').write_text(json.dumps({'lockfileVersion':3,'packages':{'node_modules/vendor':self.package}}))
        self.receipt=self.base/'CI.json';self.receipt.write_text(json.dumps({'status':'PASS','finished':'2026-10-13T01:00:00Z','head_sha':'a'*40,'worktree':str(self.owner),'commands':[{'command':'npm ci --ignore-scripts','exit_code':0}]}))
        self.entry={'path':str(self.root),'owner_head':'a'*40,'receipt':str(self.receipt),'receipt_sha256':dep.digest(self.receipt),
                    'lock_sha256':dep.digest(self.owner/'package-lock.json'),'package_sha256':dep.digest(self.owner/'package.json'),
                    'installed_lock_sha256':dep.digest(self.root/'.package-lock.json')}
        self.api=API();self.api.removed=[]
        self.ctx = [patch.object(dep,'owner_identity',return_value=None), patch.object(dep,'fresh_pins',return_value=set()),
                    patch.object(dep,'inbound_links',return_value=set()), patch.object(dep,'lsof_inactive',return_value=True)]
        for ctx in self.ctx:ctx.start();self.addCleanup(ctx.stop)

    def run_row(self, dry=True):
        return dep.retire(self.api,self.entry,dry_run=dry,deadline=time.monotonic()+30,cache=self.cache)

    def test_vendor_code_positive_dry_then_real_bounded_reclaim(self):
        row=self.run_row();self.assertEqual(row['status'],'ELIGIBLE',row)
        self.assertGreater(row['eligible_bytes'],0);self.assertEqual(row['bytes_freed'],0)
        row=self.run_row(False);self.assertEqual(row['status'],'REMOVED',row)
        self.assertGreater(row['bytes_freed'],0);self.assertFalse(self.root.exists())
        self.assertTrue(self.receipt.exists());self.assertTrue((self.owner/'package-lock.json').exists())
        self.assertEqual(self.api.removed,[str(self.root)])

    def test_actual_credentials_and_financial_payloads_never_inherit_vendor_authority(self):
        for name,data in [('credentials.json',b'{"secret":"fixture"}'),('invoice.csv',b'money'),('state',b'SQLite format 3\0'+b'x'*20)]:
            p=self.root/'vendor'/name;p.write_bytes(data)
            self.assertEqual(self.run_row(False)['status'],'REFUSED');p.unlink()
        (self.root/'vendor/credentials.js').write_bytes(b'local secret')
        self.assertEqual(self.run_row(False)['status'],'REFUSED');self.assertEqual(self.api.removed,[])

    def test_active_owner_fd_cwd_import_custody_and_new_pin_refuse(self):
        self.api.active=True;self.assertEqual(self.run_row(False)['status'],'REFUSED');self.api.active=False
        for pin in [self.owner,self.root,self.root/'vendor/credentials.js']:
            with patch.object(dep,'fresh_pins',return_value={pin}):self.assertEqual(self.run_row(False)['status'],'REFUSED')
        with patch.object(dep,'fresh_pins',side_effect=[set(),{self.root}]):self.assertEqual(self.run_row(False)['status'],'REFUSED')
        with patch.object(dep,'lsof_inactive',return_value=False):self.assertEqual(self.run_row(False)['status'],'REFUSED')
        self.assertEqual(self.api.removed,[])

    def test_symlink_reuse_escaping_link_and_identity_drift_refuse(self):
        with patch.object(dep,'inbound_links',return_value={self.root}):self.assertEqual(self.run_row(False)['status'],'REFUSED')
        (self.root/'escape').symlink_to(self.base/'outside');self.assertEqual(self.run_row(False)['status'],'REFUSED');(self.root/'escape').unlink()
        with patch.object(dep,'lsof_inactive',side_effect=lambda p: ((self.root/'vendor/cli.js').write_bytes(b'drift') and True)):
            self.assertEqual(self.run_row(False)['status'],'REFUSED')
        self.assertEqual(self.api.removed,[])

    def test_database_fixture_is_protected_even_if_published_by_package(self):
        raw=io.BytesIO()
        data=b'SQLite format 3\0'+b'x'*20
        with tarfile.open(fileobj=raw,mode='w:gz') as tf:
            for name,value in {**self.files,'fixture':data}.items():
                info=tarfile.TarInfo('package/'+name);info.size=len(value);tf.addfile(info,io.BytesIO(value))
        blob=raw.getvalue();hashed=hashlib.sha512(blob).digest();hexed=hashed.hex()
        cached=self.cache/hexed[:2]/hexed[2:4]/hexed[4:];cached.parent.mkdir(parents=True);cached.write_bytes(blob)
        self.package['integrity']='sha512-'+base64.b64encode(hashed).decode()
        for path in [self.owner/'package-lock.json',self.root/'.package-lock.json']:
            value=json.loads(path.read_text());value['packages']['node_modules/vendor']=self.package;path.write_text(json.dumps(value))
        self.entry['lock_sha256']=dep.digest(self.owner/'package-lock.json')
        self.entry['installed_lock_sha256']=dep.digest(self.root/'.package-lock.json')
        (self.root/'vendor/fixture').write_bytes(data)
        row=self.run_row(False);self.assertEqual(row['status'],'REFUSED')
        self.assertEqual(row['reason'],'protected dependency payload');self.assertEqual(self.api.removed,[])

    def test_policy_authority_and_dry_selector_are_not_generic_deletion(self):
        self.assertEqual(dep.run(self.api,{},dry_run=False,deadline=time.monotonic()+30),[])
        rows=dep.run(self.api,{'completed_ci_dependencies':[dict(self.entry,authority='old-approval')]},
                     dry_run=False,deadline=time.monotonic()+30)
        self.assertEqual(rows[0]['status'],'REFUSED');self.assertEqual(self.api.removed,[])
        import types
        with patch.dict(sys.modules,{'host_disk_pruner':types.ModuleType('host_disk_pruner')}):
            import host_disk_large_sweep as sweep
            with self.assertRaises(ValueError):
                sweep.run(self.base/'report',dry_run=False,completed_ci_only=True)

    def test_readonly_worktree_pin_projection_preserves_all_other_sources(self):
        import host_disk_auto_classes as auto
        names=('prepared_pins','pr_pins','linear_pins','live_release_pins','r8_pins')
        contexts=[patch.object(auto,'worktree_pins',return_value={self.owner})]
        contexts.extend(patch.object(auto,n,return_value={self.root}) for n in names)
        contexts.append(patch('host_disk_cold_classes.existing_pins',side_effect=lambda p:set(p)))
        for c in contexts:c.start()
        try:
            self.assertEqual(auto.external_pins(),{self.owner,self.root})
            self.assertEqual(auto.external_pins(include_worktrees=False),{self.root})
        finally:
            for c in reversed(contexts):c.stop()

    def test_absent_completion_lock_drift_missing_tar_corrupt_tar_refuse(self):
        for field in ['owner_head','receipt_sha256','lock_sha256','installed_lock_sha256']:
            old=self.entry[field];self.entry[field]='0'*64;self.assertEqual(self.run_row(False)['status'],'REFUSED');self.entry[field]=old
        blob=next(self.cache.glob('*/*/*'));blob.write_bytes(b'corrupt')
        self.assertEqual(self.run_row(False)['status'],'REFUSED');blob.unlink()
        self.assertEqual(self.run_row(False)['status'],'REFUSED');self.assertEqual(self.api.removed,[])

    def test_unsupported_roots_and_hardlinks_refuse(self):
        entry=dict(self.entry,path=str(self.owner))
        self.assertEqual(dep.retire(self.api,entry,dry_run=False,deadline=time.monotonic()+30,cache=self.cache)['status'],'REFUSED')
        os.link(self.root/'vendor/cli.js',self.base/'external-hardlink')
        self.assertEqual(self.run_row(False)['status'],'REFUSED');self.assertEqual(self.api.removed,[])


if __name__=='__main__':unittest.main()
