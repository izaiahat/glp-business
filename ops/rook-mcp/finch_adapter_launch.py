#!/home/ubuntu/.hermes/venvs/finch-mcp/bin/python
"""Dedicated Finch launcher. Metadata never upgrades; full starts use fresh cursors."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import socket
import struct
import subprocess
import sys
import types

CHECKOUT = Path('/home/ubuntu/business/.worktrees/finch-file-schema-composition')
RECEIVING = Path('/etc/finch-mcp/receiving.json')
SETUP = Path('/home/ubuntu/.hermes/state/finch-setup')
STATE = Path('/home/ubuntu/.hermes/state/finch-public-mcp')
ISSUER = 'https://dev-qv5n6hpb2umayqwt.us.auth0.com/'
AUDIENCE = 'https://hermes-01.tail9e25df.ts.net/mcp'
PORT = '18806'
REQUIRED = {'finch.requests.read', 'finch.replies.write', 'finch.channel.read'}
PYTHON = '/home/ubuntu/.hermes/venvs/finch-mcp/bin/python'
LAUNCHER = Path('/home/ubuntu/.hermes/scripts/finch_adapter_launch.py')
_SOURCE = Path(__file__).read_bytes()
_BOOTSTRAP_OK = sys._getframe().f_code == compile(_SOURCE, __file__, 'exec', dont_inherit=True, optimize=sys.flags.optimize)


def receiving_contract():
    # No application-owned JSON may replace the old literal reviewed HEAD pin.
    fds = []
    try:
        parent = None
        for index, name in enumerate(('/', *RECEIVING.parts[1:])):
            directory = index != len(RECEIVING.parts) - 1
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                         | (os.O_DIRECTORY if directory else 0), dir_fd=parent)
            fds.append((fd, parent, name, directory))
            info = os.fstat(fd)
            if (info.st_uid != 0 or info.st_mode & 0o022
                    or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                    or (not directory and info.st_nlink != 1)):
                raise ValueError('receiving_contract_custody_unsafe')
            parent = fd
        before = os.fstat(fds[-1][0])
        raw = os.pread(fds[-1][0], 4097, 0)
        if not 1 <= len(raw) <= 4096:
            raise ValueError('receiving_contract_size')
        def closed(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('duplicate_receiving_key')
                result[key] = value
            return result
        contract = json.loads(raw.decode('ascii'), object_pairs_hook=closed)
        import re
        if (set(contract) != {'schema', 'source_commit', 'source_sha256', 'launcher_sha256', 'profile_sha256', 'profile_abi_sha256', 'receiver_sha256', 'poster_sha256'}
                or contract['schema'] != 'finch-receiving-contract-v1'
                or not isinstance(contract['source_commit'], str)
                or not re.fullmatch(r'[a-f0-9]{40}', contract['source_commit'])
                or any(not isinstance(contract[k], str) or not re.fullmatch(r'[a-f0-9]{64}', contract[k])
                       for k in ('source_sha256', 'launcher_sha256', 'profile_sha256', 'profile_abi_sha256', 'receiver_sha256', 'poster_sha256'))):
            raise ValueError('receiving_contract_invalid')
        for fd, parent, name, _ in fds:
            a, b = os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (a.st_dev, a.st_ino, a.st_uid, a.st_mode) != (b.st_dev, b.st_ino, b.st_uid, b.st_mode):
                raise ValueError('receiving_contract_replaced')
        after = os.fstat(fds[-1][0])
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('receiving_contract_changed')
        return contract
    finally:
        for fd, *_ in reversed(fds): os.close(fd)


def isolate_startup():
    # systemd applies AppArmor before exec. -I -S excludes Python environment,
    # cwd/user-site and *all* sitecustomize processing before this code runs.
    if (not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode
            or sys.executable != PYTHON or Path(__file__) != LAUNCHER or not _BOOTSTRAP_OK
            or any(name.startswith(('LD_', 'PYTHON')) or name == 'GLIBC_TUNABLES' for name in os.environ)):
        raise ValueError('isolated_startup_required')
    label = Path(f'/proc/{os.getpid()}/attr/apparmor/current').read_text().strip()
    if label != 'finch-mcp-receiving-v1 (enforce)':
        raise ValueError('enforced_receiving_profile_required')
    status = {}
    for line in Path(f'/proc/{os.getpid()}/status').read_text().splitlines():
        key, sep, value = line.partition(':')
        if not sep or key in status: raise ValueError('kernel_status_invalid')
        status[key] = value.strip()
    if (any(status.get(key, '').split() != ['1000'] * 4 for key in ('Uid', 'Gid'))
            or status.get('NoNewPrivs') != '1' or status.get('Seccomp') != '2'
            or any(status.get(key) != '0000000000000000' for key in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb', 'CapBnd'))):
        raise ValueError('kernel_privilege_boundary_required')
    null, stdin = os.stat('/dev/null'), os.fstat(0)
    if not stat.S_ISCHR(stdin.st_mode) or (null.st_dev, null.st_ino) != (stdin.st_dev, stdin.st_ino):
        raise ValueError('stdin_must_be_dev_null')
    out, err = os.fstat(1), os.fstat(2)
    if not stat.S_ISSOCK(out.st_mode) or (out.st_dev, out.st_ino) != (err.st_dev, err.st_ino):
        raise ValueError('one_verified_journal_stream_required')
    with socket.fromfd(1, socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        pid, uid, gid = struct.unpack('3i', stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if (stream.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                or stream.getpeername() != '/run/systemd/journal/stdout' or uid != 0 or pid <= 0):
            raise ValueError('unverified_journal_peer')
    # The declared simple service uses no socket activation. Drop all inherited
    # nonstdio descriptors before source/config/token/Discord work, not live FDs.
    for name in os.listdir('/proc/self/fd'):
        fd = int(name)
        if fd > 2:
            try: os.close(fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise ValueError('inherited_descriptor_close_uncertain') from exc
    for name in os.listdir('/proc/self/fd'):
        if int(name) <= 2: continue
        try: os.fstat(int(name))
        except OSError as exc:
            if exc.errno == errno.EBADF: continue
            raise
        raise ValueError('unexpected_descriptor_remains')
    os.environ.clear()
    os.environ.update({'PATH': '/usr/bin:/bin', 'HOME': '/home/ubuntu',
                       'TMPDIR': '/home/ubuntu/.hermes/cache/scratch', 'LANG': 'C.UTF-8'})


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('nonsecret_config_storage_unsafe')
        return json.load(source)


def load_adapter(source_bytes, files):
    # Compile verified bytes; no stale bytecode or read/verify/import race.
    for name, _ in files:
        loaded = types.ModuleType(name)
        loaded.__file__ = str(CHECKOUT / 'scripts' / (name + '.py'))
        sys.modules[name] = loaded
        exec(compile(source_bytes[name], loaded.__file__, 'exec', dont_inherit=True), loaded.__dict__)
    return sys.modules['rook_mcp_events']


def launch(metadata_only=False, check_only=False):
    isolate_startup()
    contract = receiving_contract()
    files = [('post_discord_message', 'poster_sha256'), ('receive_one_discord_message', 'receiver_sha256'),
             ('rook_mcp_events', 'source_sha256')]
    source_bytes = {name: (CHECKOUT / 'scripts' / (name + '.py')).read_bytes() for name, _ in files}
    if (hashlib.sha256(_SOURCE).hexdigest() != contract['launcher_sha256']
            or any(hashlib.sha256(source_bytes[name]).hexdigest() != contract[key] for name, key in files)):
        raise ValueError('reviewed_launcher_or_adapter_changed')
    head_pin = contract['source_commit']
    git = ['/usr/bin/git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-c', 'core.untrackedCache=false', '-C', str(CHECKOUT)]
    git_env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_OPTIONAL_LOCKS': '0'}
    head = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True, env=git_env).strip()
    if head != head_pin or subprocess.check_output([*git, 'status', '--porcelain'], text=True, env=git_env).strip():
        raise ValueError('reviewed_source_checkout_changed')
    b = private_json(SETUP / 'nonsecret-bindings.json')
    if (b['source_commit'] != head_pin or b['tunnel_id'] != 'tunnel_6abf0b2046f08191a69a16d7c5d196c5'
            or b['guild_id'] != '1486887661099815126' or b['bot_id'] != '1555375358478655621'
            or b['finch_channel_id'] != '1555387641871081607' or b['dots_shared_channel_id'] != '1555387660644782121'
            or b['token_path'] != '/home/ubuntu/.hermes/secrets/dot-bots/finch.token'
            or set(b['allowed_sender_ids']) != {'392067741500309517', '1486880423568211968', '1554962007059472445'}):
        raise ValueError('fixed_finch_bindings_changed')
    sys.path.insert(0, str(CHECKOUT / 'scripts'))
    # Import only the existing dedicated venv's packages. Never run site.py,
    # .pth executable lines, sitecustomize or caller-selected import locations.
    packages = Path(PYTHON).parent.parent / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    if packages.is_symlink() or not packages.is_dir():
        raise ValueError('dedicated_package_directory_required')
    sys.path.append(str(packages))
    m = load_adapter(source_bytes, files)
    m._finch_confinement()
    identity = m.FinchIdentity(b['finch_channel_id'], b['dots_shared_channel_id'], b['bot_id'],
                              Path(b['token_path']), tuple(b['allowed_sender_ids']))
    argv = ['--profile', 'finch',
            '--finch-channel', identity.private_channel, '--dots-shared-channel', identity.shared_channel,
            '--finch-bot', identity.bot, '--finch-token-path', str(identity.token_path),
            '--issuer', ISSUER, '--audience', AUDIENCE, '--protected-resource-metadata-url',
            'https://hermes-01.tail9e25df.ts.net/.well-known/oauth-protected-resource/mcp', '--port', PORT]
    for sender in identity.allowed_senders:
        argv.extend(['--allowed-request-sender', sender])
    if metadata_only:
        argv.extend(['--metadata-only', '--max-run-seconds', '300'])
    else:
        auth = private_json(SETUP / 'public-auth-readback.json')
        resources = auth.get('matching_resources', [])
        if len(resources) != 1:
            raise ValueError('missing_dedicated_finch_auth0_api')
        resource = resources[0]
        if (resource.get('identifier') != AUDIENCE or resource.get('signing_alg') != 'RS256'
                or resource.get('enforce_policies') is not True
                or resource.get('token_dialect') not in {'access_token_authz', 'rfc9068_profile_authz'}
                or not REQUIRED <= {s['value'] for s in resource.get('scopes', [])}):
            raise ValueError('finch_api_rbac_or_scopes_not_verified')
        permissions = {p['permission_name'] for p in auth.get('finch_permissions', []) if p.get('resource_server_identifier') == AUDIENCE}
        if not REQUIRED <= permissions:
            raise ValueError('missing_three_operator_finch_permissions')
        user = auth.get('verified_operator', {})
        if user.get('email') != 'izaiahtilton1@gmail.com' or user.get('email_verified') is not True or not user.get('user_id'):
            raise ValueError('operator_identity_not_verified')
        if check_only:
            print(json.dumps({'status': 'FULL_CONFIGURATION_READY_FRESH_CURSORS_REQUIRED_AT_START', 'network_calls': 0}))
            return 0
        cursors = {}
        for channel in identity.channels:
            messages = m.read_messages({'channel_id': channel, 'limit': 1}, finch=identity)['structuredContent']['messages']
            if not messages:
                raise ValueError('empty_finch_channel_requires_owner_approved_baseline_message')
            cursors[channel] = messages[0]['message_id']
        identity.validate_cursors(cursors)
        argv.extend(['--operator-subject', user['user_id'], '--state-dir', str(STATE),
            '--finch-after-message', cursors[identity.private_channel],
            '--dots-shared-after-message', cursors[identity.shared_channel],
            '--overnight-pilot', '--max-run-seconds', '57600'])
        print(json.dumps({'status': 'FRESH_FINCH_CURSORS_READ', 'channels': cursors}), flush=True)
    if check_only:
        print(json.dumps({'status': 'METADATA_CONFIGURATION_READY', 'network_calls': 0}))
        return 0
    # Keep the verified interpreter, import paths, confinement and descriptors.
    # No second Python site initialization or inherited environment expansion.
    return m.main(argv)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata-only', action='store_true')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    try:
        os.umask(0o077)
        raise SystemExit(launch(args.metadata_only, args.check_only))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({'status': 'BLOCKED', 'reason': reason}), flush=True)
        raise SystemExit(78) from None
