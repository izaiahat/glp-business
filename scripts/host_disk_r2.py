"""Private R2 cold evidence; never create credentials."""
import hashlib
import json
import os
import time
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
BUCKET = 'glp-cold-evidence'
PREFIX = 'host-disk-pruner/v1/'


def credentials():
    wanted = {'CLOUDFLARE_API_TOKEN', 'CF_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID',
              'R2_ACCOUNT_ID', 'CLOUDFLARE_R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID',
              'CLOUDFLARE_R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY',
              'CLOUDFLARE_R2_SECRET_ACCESS_KEY', 'R2_ENDPOINT', 'CLOUDFLARE_R2_ENDPOINT'}
    values = {}
    path = Path('/home/ubuntu/.hermes/.env')
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().removeprefix('export ').partition('=')
            if sep and key in wanted:
                values[key] = value.strip().strip('"').strip("'")
    values.update({key: os.environ[key] for key in wanted if os.environ.get(key)})
    return values


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as file:
        for chunk in iter(lambda: file.read(1024*1024), b''):
            result.update(chunk)
    return result.hexdigest()


class Control:
    def __init__(self, values):
        self.account = values.get('CLOUDFLARE_ACCOUNT_ID')
        self.token = values.get('CLOUDFLARE_API_TOKEN') or values.get('CF_API_TOKEN')
        if not self.account or not self.token:
            raise RuntimeError('R2 control authority missing')

    def request(self, method, suffix, payload=None, missing=False):
        url = 'https://api.cloudflare.com/client/v4/accounts/' + self.account + suffix
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing:
                return None
            raise RuntimeError('R2 control HTTP ' + str(error.code)) from None
        if data.get('success') is not True:
            raise RuntimeError('R2 control refused')
        return data['result']

    def ensure(self):
        suffix = '/r2/buckets/' + BUCKET
        if self.request('GET', suffix, missing=True) is None:
            self.request('POST', '/r2/buckets', {'name': BUCKET, 'storageClass': 'Standard'})
        self.check_private()
        return {'bucket': BUCKET, 'private': True}

    def check_private(self):
        suffix = '/r2/buckets/' + BUCKET + '/domains/'
        managed = self.request('GET', suffix + 'managed')
        custom = self.request('GET', suffix + 'custom')
        if managed.get('enabled') is not False or custom.get('domains') != []:
            raise RuntimeError('R2 private-bucket proof refused')
        lifecycle = self.request('GET', '/r2/buckets/' + BUCKET + '/lifecycle')
        if any(r.get('enabled', True) and r.get('deleteObjectsTransition') for r in lifecycle.get('rules', [])):
            raise RuntimeError('R2 object-expiry custody refused')
        return True


class Destination:
    def __init__(self, client, private_check):
        self.client = client
        self.private_check = private_check

    @classmethod
    def from_environment(cls):
        import boto3
        from botocore.config import Config
        values = credentials()
        account = values.get('CLOUDFLARE_R2_ACCOUNT_ID') or values.get('R2_ACCOUNT_ID') or values.get('CLOUDFLARE_ACCOUNT_ID')
        key = values.get('CLOUDFLARE_R2_ACCESS_KEY_ID') or values.get('R2_ACCESS_KEY_ID')
        secret = values.get('CLOUDFLARE_R2_SECRET_ACCESS_KEY') or values.get('R2_SECRET_ACCESS_KEY')
        if not account or not key or not secret:
            raise RuntimeError('R2 saved S3 credentials unavailable')
        if account != values.get('CLOUDFLARE_ACCOUNT_ID'):
            raise RuntimeError('R2 account binding mismatch')
        control = Control(values)
        control.ensure()
        config = Config(signature_version='s3v4', connect_timeout=10, read_timeout=120,
                        retries={'total_max_attempts': 1}, s3={'addressing_style': 'path'})
        client = boto3.client('s3', endpoint_url='https://' + account + '.r2.cloudflarestorage.com',
                              region_name='auto', aws_access_key_id=key,
                              aws_secret_access_key=secret, config=config)
        return cls(client, control.check_private)

    def readback(self, key, deadline, missing=False):
        try:
            reply = self.client.get_object(Bucket=BUCKET, Key=key)
        except Exception as error:
            code = getattr(error, 'response', {}).get('Error', {}).get('Code')
            if missing and str(code) in {'404', 'NoSuchKey', 'NotFound'}:
                return None
            raise RuntimeError('R2 readback refused') from None
        body = reply['Body']
        result = hashlib.sha256()
        amount = 0
        try:
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError('R2 readback deadline')
                chunk = body.read(1024*1024)
                if not chunk:
                    break
                amount += len(chunk)
                result.update(chunk)
        finally:
            body.close()
        if amount != reply['ContentLength']:
            raise ValueError('R2 truncated readback')
        return {'key': key, 'bytes': amount, 'sha256': result.hexdigest(), 'verified': True}

    def upload(self, path, key, deadline):
        from boto3.s3.transfer import TransferConfig
        def progress(amount):
            if time.monotonic() > deadline:
                raise TimeoutError('R2 upload deadline')
        self.client.upload_file(str(path), BUCKET, key, Callback=progress,
            ExtraArgs={'Metadata': {'sha256': digest(path)}},
            Config=TransferConfig(multipart_threshold=64*1024**2,
                                  multipart_chunksize=64*1024**2,
                                  max_concurrency=1, use_threads=False))

    def store(self, archive, manifest, deadline):
        if self.private_check() is not True:
            raise ValueError('R2 private bucket proof missing')
        data = json.loads(Path(manifest).read_text())
        cutoff = (time.time()-30*86400)*1_000_000_000
        if not data.get('members') or any(max(r['signature'][4:6]) > cutoff for r in data['members']):
            raise ValueError('R2 requires >30d cold evidence')
        archive_hash = digest(archive)
        manifest_hash = digest(manifest)
        targets = [(archive, PREFIX+archive_hash+'.tar.zst', archive_hash, 'archive'),
                   (manifest, PREFIX+archive_hash+'-'+manifest_hash+'.manifest.json', manifest_hash, 'manifest')]
        objects = []
        for path, key, expected, role in targets:
            result = self.readback(key, deadline, missing=True)
            created = result is None
            if created:
                self.upload(path, key, deadline)
                result = self.readback(key, deadline)
            if result['sha256'] != expected or result['bytes'] != Path(path).stat().st_size:
                raise ValueError('R2 size/SHA256 readback mismatch')
            objects.append({**result, 'role': role, 'created': created})
        if self.private_check() is not True:
            raise ValueError('R2 private bucket proof missing')
        return {'bucket': BUCKET, 'private': True, 'verified': True,
                'objects': objects, 'object_count': 2,
                'object_bytes': sum(r['bytes'] for r in objects)}

    def summary(self):
        if self.private_check() is not True:
            raise ValueError('R2 private bucket proof missing')
        objects={}
        paginator=self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=BUCKET,Prefix=PREFIX):
            for row in page.get('Contents',[]):objects[row['Key']]=row['Size']
        amount=sum(objects.values())
        return {'bucket':BUCKET,'prefix':PREFIX,'private':True,
                'object_count':len(objects),'object_bytes':amount,'object_GB':amount/1e9,
                'standard_storage_monthly_USD_before_free_tier':amount/1e9*0.015}


def migrate_archives(api,m,prune_root,destination,*,pins,deadline,dry_run=True):
    """Move only producer-verified cold evidence; retain recovery controls."""
    rows=[]
    for archive in Path(prune_root).rglob('*.tar.zst'):
        if time.monotonic()+1>=deadline:break
        manifest=Path(str(archive)+'.manifest.json')
        verified=Path(str(archive)+'.verified.json')
        ledger=Path(str(archive)+'.retirement.jsonl')
        if not all(p.is_file() and not p.is_symlink() for p in (archive,manifest,verified,ledger)):continue
        row={'rule':'r2_verified_archive_migration','path':str(archive),'bytes_freed':0}
        try:
            info=json.loads(verified.read_text());data=json.loads(manifest.read_text())
            members=data['members'];source=Path(data['source'])
            cutoff=(time.time()-30*86400)*1e9
            if any(max(r['signature'][4:6])>cutoff for r in members):continue
            if archive.parent.resolve()!=archive.parent.absolute() or archive.stat().st_nlink!=1:
                raise ValueError('archive alias/hardlink custody')
            fresh=m.custody_pins(pins)
            if m.protected(archive.parent,fresh) or any(m.protected(source/r['name'],fresh) for r in members):
                raise ValueError('source/reference custody')
            if any(archive==q or q in archive.parents for q in fresh) or not m.inactive(api,archive):
                raise ValueError('archive reference/activity')
            if info.get('verified') is not True or digest(archive)!=info['archive_sha256'] or digest(manifest)!=info['manifest_sha256']:
                raise ValueError('local verification drift')
            expected={r['name']:r['sha256'] for r in members}
            for line in ledger.read_text().splitlines():
                retired=json.loads(line)
                if expected.get(retired['member'])!=retired['sha256']:raise ValueError('retirement journal drift')
            if dry_run:
                rows.append(dict(row,status='ELIGIBLE'));continue
            identity=m.signature(archive)
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR','/home/ubuntu/.hermes/cache/scratch')) as temp:
                m.verify_archive(archive,members,Path(temp)/'listing.txt')
            receipt=destination.store(archive,manifest,deadline)
            pair={r['role']:r for r in receipt.get('objects',[])}
            if receipt.get('verified') is not True or receipt.get('private') is not True or set(pair)!={'archive','manifest'}:
                raise ValueError('remote proof missing')
            if pair['archive']['sha256']!=info['archive_sha256'] or pair['manifest']['sha256']!=info['manifest_sha256'] or pair['archive']['bytes']!=archive.stat().st_size or pair['manifest']['bytes']!=manifest.stat().st_size or any(r.get('verified') is not True for r in pair.values()):
                raise ValueError('remote binding drift')
            fresh=m.custody_pins(pins)
            if m.signature(archive)!=identity or any(archive==q or q in archive.parents for q in fresh) or not m.inactive(api,archive):
                raise ValueError('new archive custody/drift')
            info['remote']=receipt;m.write_json(verified,info)
            amount=archive.stat().st_blocks*512
            archive.unlink()
            rows.append(dict(row,status='MIGRATED_R2_VERIFIED',bytes_freed=amount,remote=receipt))
        except Exception as error:rows.append(dict(row,status='REFUSED',reason=type(error).__name__))
    return rows







