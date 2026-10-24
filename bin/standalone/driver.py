#!/usr/bin/python3 -I
"""Standalone Decky NVIDIA A/B controller. No installed NVIDIA helper required.

Staging/rollback design adapted from steamos-nvidia-installer's driver-change.py
and driver-stage.sh. See LICENSE.steamos-nvidia. CLI writes require root.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
STATE = Path('/home/.decky-nvidia-update')
CUSTOM = STATE / 'custom'
CORE_PACKAGES = ('nvidia-utils', 'nvidia-open-dkms', 'lib32-nvidia-utils')
RUNTIME = Path('/usr/lib/decky-nvidia-update')
BTRFS_SYSFS = Path('/sys/fs/btrfs')
VERSION = re.compile(r'[0-9]+(?:\.[0-9]+)*(?:-[0-9]+(?:\.[0-9]+)*)?')
MANIFEST = 'etc/steamos-atomupd/manifest.json'
HOOK_START = '# decky-nvidia-update hook v1\n'
HOOK_END = '# decky-nvidia-update hook end\n'
NATIVE_START = '# steamos-nvidia shared-update-hook v1\n'
NATIVE_END = '# steamos-nvidia shared-update-hook end\n'
ANCHORS = tuple(f'(( ERR == 0 )) && steamos-bootconf --image $UPDATED_SLOT set-mode {mode}'
                for mode in ('reboot', 'first-boot')) + (
                    '(( ERR == 0 )) && holo-bootconf --image $UPDATED_SLOT set-mode first-boot',)
FINALIZE_DIRECT = 'steamos-chroot --partset $UPDATED_SLOT -- steamos-finalize-install --no-kernel'
# Match the complete fallback loop, including its failure check. Recognizing
# just the chroot call could place the repair inside a loop or before an error.
FINALIZE_LOOP = '''declare -ar FINISH=({holo,steamos}-finalize-install steamos-boot-install)
declare -i  FINISHED=0

for finish in ${FINISH[@]}
do
    if holo-chroot --partset $UPDATED_SLOT -- $finish --no-kernel
    then
        FINISHED=1
        break
    fi
done

if [ $FINISHED -ne 1 ]
then
    err "Failed to install bootloaders (tried: ${FINISH[*]})"
fi'''
FINALIZERS = (FINALIZE_DIRECT, FINALIZE_LOOP,
              FINALIZE_LOOP.replace('if holo-chroot ', 'if steamos-chroot '))


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError(f'Unsafe transaction directory: {path}')


def write_json(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def read_state():
    if not STATE.exists():
        return {'status': 'idle'}
    private_dir(STATE)
    path = STATE / 'transaction.json'
    if not path.exists():
        return {'status': 'idle'}
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Unsafe transaction record')
    return json.loads(path.read_text())


def remove_hook(text, start, end):
    if start not in text and end not in text:
        return text
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError('Unrecognized existing NVIDIA hook')
    begin = text.index(start)
    finish = text.index(end) + len(end)
    if finish <= begin:
        raise ValueError('Invalid NVIDIA hook ordering')
    return text[:begin] + text[finish:]


def hook_text(text, command, temporary=False):
    """Accept the inspected modern Valve finalization/activation ordering only."""
    if not text.startswith('#!/bin/bash\n'):
        raise ValueError('Unsupported Valve post-install interpreter')
    text = remove_hook(text, HOOK_START, HOOK_END)
    if temporary:
        text = remove_hook(text, NATIVE_START, NATIVE_END)
    elif NATIVE_START in text:
        # The native integration owns OS repair and consumes the preserved pins.
        return text
    anchors = [a for a in ANCHORS if a in text]
    finalizers = [f for f in FINALIZERS if f in text]
    if (len(anchors) != 1 or text.count(anchors[0]) != 1
            or len(finalizers) != 1 or text.count(finalizers[0]) != 1):
        raise ValueError('Unsupported Valve update script; no disk changes made')
    anchor = anchors[0]
    finalize = finalizers[0]
    if text.index(finalize) + len(finalize) > text.index(anchor):
        raise ValueError('Unexpected Valve finalization order')
    block = HOOK_START + '''if (( ERR == 0 )); then
    [[ "$UPDATED_SLOT" == A || "$UPDATED_SLOT" == B ]] || fail "Unknown NVIDIA slot"
    [[ "$UPDATED_SLOT" != "$BOOTED_SLOT" ]] || fail "NVIDIA target is active"
    steamos-bootconf --image "$UPDATED_SLOT" config --no-create --set image-invalid 1 --set boot-requested-at 0 --set boot-attempts 0 || fail "Cannot protect NVIDIA slot"
    sync -f /esp/SteamOS/conf || fail "Cannot sync NVIDIA slot protection"
    ''' + command + ''' || fail "NVIDIA preparation failed; target remains disabled"
fi
''' + HOOK_END
    if 'holo-bootconf' in anchor:
        block = block.replace('    steamos-bootconf ', '    holo-bootconf ')
    text = text.replace(anchor, block + anchor)
    guard = '''# decky-nvidia-update mount guard v1
if [[ ${DECKY_NVIDIA_UPDATE_NS:-0} != 1 ]]; then
    exec /usr/bin/unshare --mount --propagation slave /usr/bin/env DECKY_NVIDIA_UPDATE_NS=1 /bin/bash "$0" "$@"
fi
unset DECKY_NVIDIA_UPDATE_NS
/usr/bin/mount -o remount,bind,noatime /var || exit 1
/usr/bin/mount -o remount,bind,noatime /etc || exit 1
# decky-nvidia-update mount guard end
'''
    if 'decky-nvidia-update mount guard' in text and text.count(guard) != 1:
        raise ValueError('Unrecognized update mount guard')
    if not temporary and guard not in text and 'steamos-nvidia update-mount-guard' not in text:
        text = text.replace('#!/bin/bash\n', '#!/bin/bash\n' + guard, 1)
    return text


def root_identity(root, uuid):
    root = Path(root)
    h = hashlib.sha256()
    h.update(uuid.encode())
    h.update((root / MANIFEST).read_bytes())
    # Include package metadata and every installed NVIDIA module, without
    # executing programs from an inactive system while checking rollback.
    for path in sorted((root / 'usr/lib/holo/pacmandb/local').glob('nvidia-utils-[0-9]*/desc')):
        h.update(path.read_bytes())
    modules = sorted((root / 'usr/lib/modules').glob('*/updates/dkms/nvidia*.ko*'))
    for path in modules:
        h.update(path.relative_to(root).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def mounted_root_device():
    # Btrfs exposes an anonymous filesystem MAJ:MIN in mountinfo (e.g. 0:27),
    # not the backing block device (e.g. 8:4). Resolve its mounted device via
    # the kernel's Btrfs registry; only single-device roots are supported.
    if run('findmnt', '-rn', '--mountpoint', '/', '-o', 'FSTYPE') != 'btrfs':
        raise ValueError('The running root must use Btrfs')
    fsid = run('findmnt', '-rn', '--mountpoint', '/', '-o', 'UUID')
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', fsid):
        raise ValueError('Cannot identify the mounted Btrfs root')
    members = list((BTRFS_SYSFS / fsid / 'devices').iterdir())
    if len(members) != 1:
        raise ValueError('The running root must use exactly one block device')
    device = (members[0] / 'dev').read_text().strip()
    if not re.fullmatch(r'[0-9]+:[0-9]+', device):
        raise ValueError('Cannot identify the Btrfs root device')
    return device


def require_image_released(image, fsid, timeout=10):
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', fsid):
        raise ValueError('Invalid build image UUID')
    deadline = time.monotonic() + timeout
    while True:
        # This UUID belongs to our freshly created image. Its registration must
        # disappear before copying its bytes to another device: otherwise Btrfs
        # assigns a temporary FSID and /etc overlay origin checks can fail.
        loops = run('losetup', '-j', str(image))
        if not loops and not (BTRFS_SYSFS / fsid).exists():
            return
        if time.monotonic() >= deadline:
            raise ValueError('Build root image is still in use; refusing to write the inactive slot')
        time.sleep(0.1)


def layout():
    source = run('steamos-bootconf', 'this-image')
    if source not in ('A', 'B'):
        raise ValueError('An installed SteamOS A/B system is required')
    target = 'B' if source == 'A' else 'A'
    devices, ids, parents = {}, set(), set()
    for slot in (source, target):
        for part in ('rootfs', 'var', 'efi'):
            path = Path(f'/dev/disk/by-partsets/{slot}/{part}').resolve(strict=True)
            info = path.stat()
            if not stat.S_ISBLK(info.st_mode) or info.st_rdev in ids:
                raise ValueError('A/B devices are missing or overlap')
            ids.add(info.st_rdev)
            if run('lsblk', '-dn', '-o', 'PARTLABEL', str(path)) != f'{part}-{slot}':
                raise ValueError('A/B partition label mismatch')
            parent = run('lsblk', '-dn', '-o', 'PKNAME', str(path))
            if not parent:
                raise ValueError('Cannot identify the system disk')
            parents.add(parent)
            devices[f'{slot}/{part}'] = str(path)
    if len(parents) != 1:
        raise ValueError('A/B partitions must belong to the same system disk')
    active = mounted_root_device()
    if active != run('lsblk', '-dn', '-o', 'MAJ:MIN', devices[f'{source}/rootfs']):
        raise ValueError('The boot slot does not match the mounted root')
    for part in ('rootfs', 'var', 'efi'):
        result = subprocess.run(['findmnt', '-rn', '-S', devices[f'{target}/{part}']],
                                capture_output=True)
        if result.returncode != 1:
            raise ValueError(f'Inactive {part} is mounted or its state is unknown')
    return source, target, devices


def preflight():
    release = Path('/etc/os-release').read_text()
    if not re.search(r'^ID=(?:steamos|"steamos")$', release, re.M):
        raise ValueError('Only SteamOS is supported')
    for command in ('btrfs', 'mkfs.btrfs', 'mkfs.ext4', 'steamos-bootconf', 'steamos-chroot',
                    'atomupd-manager', 'busctl', 'systemctl', 'unshare', 'rsync', 'modinfo',
                    'losetup', 'fsfreeze', 'findmnt', 'lsblk', 'blockdev', 'curl', 'chroot',
                    'pacman', 'tar', 'zstd', 'readelf', 'flock'):
        if not shutil.which(command):
            raise ValueError(f'Missing system tool: {command}')
    source, target, devices = layout()
    if run('steamos-bootconf', 'selected-image') != source:
        raise ValueError('Another system is selected; reboot before preparing a driver')
    if run('btrfs', 'property', 'get', '/', 'ro') != 'ro=true':
        raise ValueError('The running root must be read-only')
    if run('findmnt', '-n', '-o', 'TARGET', '--mountpoint', '/home') != '/home':
        raise ValueError('/home must be a separate filesystem')
    if run('atomupd-manager', 'get-update-status') != 'idle':
        raise ValueError('Finish pending SteamOS updates first')
    if run('busctl', 'get-property', 'de.pengutronix.rauc', '/',
           'de.pengutronix.rauc.Installer', 'Operation') != 's "idle"':
        raise ValueError('The system updater is busy')
    post = Path('/usr/lib/rauc/post-install.sh').read_text()
    hook_text(post, '/bin/true', temporary=True)
    subprocess.run([sys.executable, '-I', str(HERE / 'shutdown.py'), 'check'], check=True)
    size = int(run('blockdev', '--getsize64', devices[f'{target}/rootfs']))
    if size < 2 * 1024**3:
        raise ValueError('Invalid target root size')
    required = size + 12 * 1024**3
    available = shutil.disk_usage('/home').free
    return dict(source=source, target=target, devices=devices, target_bytes=size,
                required_bytes=required, available_bytes=available,
                enough_space=available >= required)


def protect(slot):
    if slot not in ('A', 'B'):
        raise ValueError('Invalid slot')
    run('steamos-bootconf', '--image', slot, 'config', '--no-create',
        '--set', 'image-invalid', '1', '--set', 'boot-requested-at', '0', '--set', 'boot-attempts', '0')
    run('sync', '-f', '/esp/SteamOS/conf')


def transaction_status():
    data = read_state()
    data['active_slot'] = run('steamos-bootconf', 'this-image')
    data['selected_slot'] = run('steamos-bootconf', 'selected-image')
    expected = (data.get('source') if data.get('status') == 'rollback-selected'
                else data.get('target') if data.get('status') == 'ready' else None)
    data['pending_reboot'] = (expected in ('A', 'B') and
                              data['selected_slot'] == expected and
                              data['active_slot'] in ('A', 'B') and
                              data['active_slot'] != expected)
    return data


def install(version, operation_id=None, custom=False):
    if version != 'latest' and not VERSION.fullmatch(version):
        raise ValueError('Invalid driver version')
    if not custom:
        print('[phase] preflight Checking the system', flush=True)
    info = preflight()
    if not info['enough_space']:
        raise ValueError(f"Need {info['required_bytes'] // 1024**2} MiB free on /home")
    private_dir(STATE)
    previous = read_state()
    with tempfile.TemporaryDirectory(prefix='prepare-', dir=STATE) as work:
        work = Path(work)
        info.update(status='preparing', requested=version, work=str(work), operation_id=operation_id)
        if custom:
            write_json(work / 'custom-pins.json', custom_pins(version))
        uuid = run('blkid', '-s', 'UUID', '-o', 'value', info['devices'][info['source'] + '/rootfs'])
        info['source_uuid'] = uuid
        info['source_identity'] = root_identity('/', uuid)
        write_json(work / 'request.json', info)
        write_json(STATE / 'transaction.json', info)
        try:
            # Arm in the host namespace, before Valve writes the shutdown sync
            # trigger. /run survives staging but expires at the next boot.
            subprocess.run([sys.executable, '-I', str(HERE / 'shutdown.py'), 'install'], check=True)
            subprocess.run(['unshare', '--mount', '--propagation', 'slave', '/bin/bash',
                            str(HERE / 'stage.sh'), str(work)], check=True)
            ready = json.loads((work / 'ready.json').read_text())
            if run('steamos-bootconf', 'selected-image') != info['target']:
                raise ValueError('Prepared system was not selected')
            info.update(ready, status='ready')
            print('Prepared driver. Reboot to load it; the previous system is retained.', flush=True)
        except BaseException:
            if (work / 'writing').exists():
                protect(info['target'])
            info['status'] = 'failed'
            if not (work / 'writing').exists() and previous.get('status') == 'ready':
                info = previous
            raise
        finally:
            info.pop('work', None)
            write_json(STATE / 'transaction.json', info)


def prune(root, wanted_file):
    root = Path(root).resolve()
    if root == Path('/'):
        raise ValueError('Refusing to prune the running root')
    wanted = set(Path(wanted_file).read_text().splitlines())
    db = root / 'usr/lib/holo/pacmandb/local'
    obsolete, owned = set(), set()
    for manifest in db.glob('*/files'):
        driver = bool(re.fullmatch(r'(?:nvidia-utils|lib32-nvidia-utils)-[0-9].*', manifest.parent.name))
        recording = False
        for line in manifest.read_text().splitlines():
            if line.startswith('%'):
                recording = line == '%FILES%'
                continue
            if not recording or not line or line.endswith('/'):
                continue
            rel = Path(line)
            if rel.is_absolute() or '..' in rel.parts:
                raise ValueError('Invalid path in package manifest')
            if not driver:
                owned.add(line)
            elif line not in wanted:
                obsolete.add(line)
    paths = []
    for rel in obsolete - owned:
        path = root / rel
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError('Package path escapes target')
        if path.is_file() or path.is_symlink():
            paths.append(path)
    for path in paths:
        path.unlink()


def record(root, version, kernel, urls):
    data = dict(version=version, kernel=kernel, urls=urls)
    package_pins(data)
    data['sha256'] = {url: local_package(url) for url in urls.split() if url.startswith('file://')}
    root = Path(root)
    runtime = root / RUNTIME.relative_to('/')
    runtime.mkdir(parents=True, exist_ok=True)
    for path in HERE.iterdir():
        if path.suffix in ('.py', '.sh') or path.name == 'LICENSE.steamos-nvidia':
            shutil.copyfile(path, runtime / path.name)
            os.chmod(runtime / path.name, 0o644)
    write_json(runtime / 'installed.json', data)
    native = root / 'usr/lib/steamos-nvidia/driver.conf'
    if native.exists():
        # Keep TRIM_CUDA, xpadneo and future integration fields intact.
        text = native.read_text()
        for key, value in dict(DRIVER_SPEC=version, DRIVER_VERSION=version, PKG_URLS=urls).items():
            text, count = re.subn(r'^' + key + r'=.*$', lambda m: key + '=' + shlex.quote(value), text, flags=re.M)
            if count != 1:
                raise ValueError(f'Unsupported native driver config: {key}')
        native.write_text(text)
        (native.parent / 'complete').unlink(missing_ok=True)
    post = root / 'usr/lib/rauc/post-install.sh'
    text = hook_text(post.read_text(), '/bin/bash /usr/lib/decky-nvidia-update/repair.sh "$UPDATED_SLOT"')
    post.write_text(text)


def package_pins(data):
    version, urls = data['version'], data['urls']
    if not VERSION.fullmatch(version) or '-' not in version:
        raise ValueError('Expected an exact package version')
    if not urls.strip():
        raise ValueError('Only archived/mirrored package sources are supported')
    packages = []
    for url in urls.split():
        if url.startswith('file://'):
            digest = local_package(url)
            if 'sha256' in data and data['sha256'].get(url) != digest:
                raise ValueError('Saved custom package checksum mismatch')
        elif not re.fullmatch(r'https://(?:archive\.archlinux\.org/packages/[a-z0-9]/[a-z0-9-]+|geo\.mirror\.pkgbuild\.com/(?:extra|multilib)/os/x86_64)/[a-zA-Z0-9.+_-]+\.pkg\.tar\.zst', url):
            raise ValueError('Only archived/mirrored or retained custom packages are supported')
        filename = url.rsplit('/', 1)[1]
        match = re.fullmatch(r'([a-z0-9-]+)-([0-9.]+-[0-9.]+)-x86_64\.pkg\.tar\.zst', filename)
        if not match:
            raise ValueError('Unsupported package filename')
        packages.append((match[1], match[2], filename, url))
    names = [p[0] for p in packages]
    if len(set(names)) != len(names) or any(p not in names for p in ('nvidia-utils', 'nvidia-open-dkms', 'lib32-nvidia-utils')):
        raise ValueError('Incomplete or duplicate NVIDIA package set')
    for name, ver, _, _ in packages:
        if name == 'nvidia-utils' and ver != version:
            raise ValueError('nvidia-utils pin mismatch')
        if name in ('nvidia-open-dkms', 'lib32-nvidia-utils') and ver.rsplit('-', 1)[0] != version.rsplit('-', 1)[0]:
            raise ValueError('Companion driver version mismatch')
    return packages


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def custom_manifest(version):
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', version):
        raise ValueError('Expected a bare NVIDIA version')
    for path in (STATE, CUSTOM, CUSTOM / version):
        if not path.exists():
            raise ValueError('Retained custom build is missing')
        private_dir(path)
    directory = CUSTOM / version
    manifest = directory / 'build.json'
    secure_file(manifest)
    data = json.loads(manifest.read_text())
    expected = [f'{pkg}-{version}-1-x86_64.pkg.tar.zst' for pkg in CORE_PACKAGES]
    if data.get('version') != version or data.get('packages') != expected or set(data.get('sha256', {})) != set(expected):
        raise ValueError('Incomplete custom package manifest')
    for pkg, name in zip(CORE_PACKAGES, expected):
        if not (directory / pkg).exists():
            raise ValueError('Retained custom package directory is missing')
        private_dir(directory / pkg)
        secure_file(directory / pkg / name)
        if not re.fullmatch(r'[0-9a-f]{64}', data['sha256'][name]):
            raise ValueError('Invalid custom package checksum')
    return data


def secure_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f'Unsafe retained package file: {path}')


def local_package(url):
    # No arbitrary file URLs. Packages are immutable, root-owned retained builds.
    pattern = re.escape('file://' + CUSTOM.as_posix()) + r'/([0-9]+(?:\.[0-9]+)+)/(nvidia-utils|nvidia-open-dkms|lib32-nvidia-utils)/([a-z0-9.+_-]+\.pkg\.tar\.zst)'
    match = re.fullmatch(pattern, url)
    if not match:
        raise ValueError('Custom package is outside the retained build directory')
    version, pkg, name = match.groups()
    data = custom_manifest(version)
    if name != f'{pkg}-{version}-1-x86_64.pkg.tar.zst' or name not in data['packages']:
        raise ValueError('Package is not in the retained build manifest')
    digest = file_hash(CUSTOM / version / pkg / name)
    if digest != data['sha256'][name]:
        raise ValueError('Retained custom package checksum mismatch')
    return digest


def custom_pins(version):
    manifest = custom_manifest(version)
    # Keep /<package>/ in each URL, as the native desktop updater uses this
    # component to distinguish replaceable driver pins from support libraries.
    urls = ['file://' + (CUSTOM / version / pkg / name).as_posix()
            for pkg, name in zip(CORE_PACKAGES, manifest['packages'])]
    data = dict(version=version + '-1', urls=' '.join(urls),
                sha256=dict(zip(urls, (manifest['sha256'][name] for name in manifest['packages']))))
    package_pins(data)
    return data


def list_custom():
    if not CUSTOM.exists():
        return []
    private_dir(STATE)
    private_dir(CUSTOM)
    result = []
    for path in CUSTOM.iterdir():
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', path.name):
            continue
        try:
            result.append(custom_manifest(path.name))
        except (ValueError, OSError, KeyError):
            continue
    return sorted(result, key=lambda d: tuple(map(int, d['version'].split('.'))), reverse=True)


def build_custom(version):
    print('[phase] custom Preparing custom driver packages', flush=True)
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', version) or int(version.split('.')[0]) < 590:
        raise ValueError('Expected an NVIDIA version from 590 onward')
    private_dir(STATE)
    private_dir(CUSTOM)
    target = CUSTOM / version
    if target.exists():
        custom_pins(version)  # Never replace packages pinned by either OS slot.
        print('[1/8] Using retained custom packages for ' + version, flush=True)
        return
    info = preflight()
    if not info['enough_space'] or info['available_bytes'] < 15 * 1024**3:
        raise ValueError('Insufficient space on /home for a custom build')
    with tempfile.TemporaryDirectory(prefix='prepare-custom-', dir=STATE) as temp:
        work = Path(temp)
        output = work / 'packages'
        output.mkdir(mode=0o700)
        subprocess.run(['/bin/bash', str(HERE / 'build-custom.sh'), version, str(work)], check=True)
        names = [f'{pkg}-{version}-1-x86_64.pkg.tar.zst' for pkg in CORE_PACKAGES]
        for pkg, name in zip(CORE_PACKAGES, names):
            path = output / pkg / name
            secure_file(path)
            metadata = run('tar', '-xOf', str(path), '.PKGINFO')
            for key, value in (('pkgname', pkg), ('pkgver', version + '-1'), ('arch', 'x86_64')):
                if re.findall(r'^' + key + r' = (.+)$', metadata, re.M) != [value]:
                    raise ValueError('Unexpected custom package metadata: ' + name)
        data = dict(version=version, built_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    packages=names, sha256={name: file_hash(output / pkg / name)
                                           for pkg, name in zip(CORE_PACKAGES, names)})
        write_json(output / 'build.json', data)
        for path in output.rglob('*'):
            if not path.is_file():
                continue
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
        os.rename(output, target)
        run('sync', '-f', str(CUSTOM))
    custom_pins(version)
    print('[8/8] Custom packages retained for installation and future OS updates', flush=True)


def rollback(operation_id=None):
    print('[phase] preflight Checking the saved systems', flush=True)
    info = read_state()
    if info.get('status') != 'ready':
        raise ValueError('No completed driver preparation is available')
    source, target, devices = layout()
    if {info.get('source'), info.get('target')} != {'A', 'B'}:
        raise ValueError('Invalid saved transaction')
    if devices != info['devices']:
        raise ValueError('The system disk layout has changed')
    if run('atomupd-manager', 'get-update-status') != 'idle':
        raise ValueError('Finish pending SteamOS updates first')
    # Validate BOTH systems before changing boot selection, also on cancellation.
    print('[phase] verify Verifying and selecting the previous system', flush=True)
    subprocess.run(['unshare', '--mount', '--propagation', 'slave', '/bin/bash',
                    str(HERE / 'rollback.sh'), str(STATE / 'transaction.json')], check=True)
    info['status'] = 'rollback-selected'
    info['operation_id'] = operation_id
    write_json(STATE / 'transaction.json', info)


def cache_maintenance(lock, collect=True):
    """Best effort, after a durable success or before reusing a legacy build.

    Keep the shared updater lock alive in the worker, even if Decky reloads.
    Cache errors must never change a completed driver's transaction outcome.
    """
    try:
        command = ['unshare', '--mount', '--propagation', 'private', '/usr/bin/python3',
                   '-I', str(HERE / 'cache.py'), '--lock-fd', str(lock.fileno())]
        if not collect:
            command.append('--migrate-only')
        result = subprocess.run(command, pass_fds=(lock.fileno(),), stdout=subprocess.PIPE,
                                text=True, check=True)
        return json.loads(result.stdout)
    except Exception as error:
        print('[cache] Maintenance deferred: ' + str(error), file=sys.stderr, flush=True)
        return dict(deferred=True, reason=str(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation-id', default=uuid.uuid4().hex)
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('install', 'install-custom', 'build'):
        sub.add_parser(name).add_argument('version')
    for name in ('preflight', 'status', 'rollback', 'custom-list', 'cache-migrate', 'cache-cleanup'):
        sub.add_parser(name)
    p = sub.add_parser('prune'); p.add_argument('root'); p.add_argument('wanted')
    p = sub.add_parser('record')
    for arg in ('root', 'version', 'kernel', 'urls'):
        p.add_argument(arg)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{32}', args.operation_id):
        raise ValueError('Invalid operation ID')
    if os.geteuid() != 0:
        raise ValueError('Run as root')
    if args.action == 'prune':
        prune(args.root, args.wanted)
    elif args.action == 'record':
        record(args.root, args.version, args.kernel, args.urls)
    elif args.action == 'status':
        print(json.dumps(transaction_status()))
    elif args.action == 'preflight':
        print(json.dumps(preflight()))
    elif args.action == 'custom-list':
        print(json.dumps(list_custom()))
    else:
        import fcntl
        with open('/run/steamos-nvidia-update.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action in ('cache-migrate', 'cache-cleanup'):
                print(json.dumps(cache_maintenance(lock, collect=args.action == 'cache-cleanup')))
            elif args.action in ('install-custom', 'build'):
                cache_maintenance(lock, collect=False)
                build_custom(args.version)
                if args.action == 'install-custom':
                    install(args.version, args.operation_id, custom=True)
                    cache_maintenance(lock)
            elif args.action == 'install':
                install(args.version, args.operation_id)
                cache_maintenance(lock)
            else:
                rollback(args.operation_id)
                cache_maintenance(lock)


if __name__ == '__main__':
    os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    os.environ['LC_ALL'] = 'C'
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'[fail] {error}', file=sys.stderr)
        sys.exit(1)
