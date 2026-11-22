"""Root-only integration test; run in a private mount namespace.

Uses only new temporary image files and their verified loop devices. Reproduces
the real Btrfs-lower/ext4-upper failure, then exercises production payload code.
"""
from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import tempfile


def run(*args):
    return subprocess.check_output([str(a) for a in args], stderr=subprocess.STDOUT, text=True).strip()


assert os.geteuid() == 0
assert os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt'), 'Use unshare --mount --propagation private'
root = Path(tempfile.mkdtemp(prefix='pacman-xattr-'))
mounts, loops = [], []
payload = (Path(__file__).resolve().parents[1] / 'bin/standalone/payload.sh').read_text()
block = payload[payload.index('BUILD_ETC=${BUILD_ETC:-'):payload.index('mount -t proc proc "$MERGED/proc"')]
shell = 'die(){ echo "$*" >&2; exit 1; }; BUILD_ETC=$1; MERGED=$2; TARGET=$3;\n'
old = '''rm -f "$MERGED/etc/pacman.conf"
cp -a "$BUILD_ETC/pacman.conf" "$MERGED/etc/pacman.conf"
mkdir -p "$MERGED/etc/pacman.d"
rsync -aHAX --delete --numeric-ids "$BUILD_ETC/pacman.d/" "$MERGED/etc/pacman.d/"
'''


def image(name, kind, size):
    path = root / (name + '.img')
    with path.open('xb') as stream:
        stream.truncate(size * 1024**2)
    device = run('losetup', '--find', '--show', path)
    loops.append(device)
    assert Path(run('losetup', '-n', '-O', 'BACK-FILE', device)).resolve() == path.resolve()
    run('mkfs.' + kind, '-f' if kind == 'btrfs' else '-F', device)
    dest = root / name
    dest.mkdir()
    run('mount', device, dest)
    mounts.append(dest)
    return dest


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def overlay(lower, backing, label):
    upper, work = backing / (label + '-upper'), backing / (label + '-work')
    merged = root / label
    for p in (upper, work, merged):
        p.mkdir()
    run('mount', '-t', 'overlay', 'overlay', '-o', f'index=off,lowerdir={lower},upperdir={upper},workdir={work}', merged)
    mounts.append(merged)
    return merged


try:
    lower = image('lower', 'btrfs', 256)
    backing = image('upper', 'ext4', 96)
    put(lower/'etc/pacman.conf', '[jupiter-new-os]\n')
    put(lower/'etc/pacman.d/mirrorlist', 'stock-mirror\n')
    for p in [lower/'etc/pacman.d', lower/'etc/pacman.conf', lower/'etc/pacman.d/mirrorlist']:
        run('btrfs', 'property', 'set', p, 'compression', 'zstd')
        assert os.getxattr(p, 'btrfs.compression') == b'zstd'
    host = backing / 'host-etc'
    put(host/'pacman.conf', '[jupiter-live]\n')
    put(host/'pacman.d/mirrorlist', 'live-mirror\n')
    put(host/'pacman.d/gnupg/private-keys-v1.d/test.key', 'fake-build-key')
    (host/'pacman.d/mirror-link').symlink_to('mirrorlist')
    os.link(host/'pacman.d/mirrorlist', host/'pacman.d/mirror-hardlink')
    os.chmod(host/'pacman.d/gnupg/private-keys-v1.d', 0o700)
    for source in [host/'pacman.conf', host/'pacman.d/mirrorlist', lower/'etc/pacman.conf', lower/'etc/pacman.d/mirrorlist']:
        os.setxattr(source, 'user.fixture', b'preserve-me')
        run('setfacl', '-m', 'u:12345:r', source)
    original = {str(p.relative_to(lower)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in lower.rglob('*') if p.is_file()}
    broken = overlay(lower, backing, 'old-copy')
    print('Kernel: ' + run('uname','-r') + '; rsync: ' + run('rsync','--version').splitlines()[0], flush=True)
    print('Lower xattrs: ' + repr(os.listxattr(lower/'etc/pacman.d')) + '; overlay xattrs: ' + repr(os.listxattr(broken/'etc/pacman.d')), flush=True)
    result = subprocess.run(['bash','-euc',shell+old,'_',str(host),str(broken),str(lower)], capture_output=True, text=True)
    assert result.returncode == 23 and 'btrfs.compression' in result.stderr and 'Operation not supported' in result.stderr, result
    print('PASS: old copy reproduces rsync code23 / btrfs.compression EOPNOTSUPP', flush=True)
    run('umount',broken)
    mounts.remove(broken)
    put(lower/'etc/pacman.d/obsolete', 'remove-in-driver-build\n')
    original['etc/pacman.d/obsolete'] = hashlib.sha256((lower/'etc/pacman.d/obsolete').read_bytes()).hexdigest()
    for mode, source in [('driver',host), ('os-repair',lower/'etc')]:
        merged = overlay(lower, backing, 'fixed-' + mode)
        run('bash','-euc',shell+block,'_',str(source) if mode=='driver' else '',merged,lower)
        for name in ['pacman.conf','pacman.d/mirrorlist']:
            dest = merged/'etc'/name
            src = source/name
            assert dest.read_bytes() == src.read_bytes()
            assert os.getxattr(dest,'user.fixture') == b'preserve-me'
            assert run('getfacl','-cpn',dest) == run('getfacl','-cpn',src)
            assert dest.stat().st_mode == src.stat().st_mode
            assert (dest.stat().st_uid,dest.stat().st_gid) == (src.stat().st_uid,src.stat().st_gid)
        if mode=='driver':
            assert not (merged/'etc/pacman.d/obsolete').exists()
            assert os.readlink(merged/'etc/pacman.d/mirror-link') == 'mirrorlist'
            assert (merged/'etc/pacman.d/mirror-hardlink').stat().st_ino == (merged/'etc/pacman.d/mirrorlist').stat().st_ino
            assert (merged/'etc/pacman.d/gnupg/private-keys-v1.d/test.key').read_text() == 'fake-build-key'
            assert (merged/'etc/pacman.d/gnupg/private-keys-v1.d').stat().st_mode & 0o777 == 0o700
        assert not (lower/'etc/pacman.d/gnupg').exists()
        assert original == {str(p.relative_to(lower)):hashlib.sha256(p.read_bytes()).hexdigest() for p in lower.rglob('*') if p.is_file()}
        print(f'PASS: {mode} copies production pacman config, ACLs, user xattrs and permissions; immutable lower unchanged',flush=True)
finally:
    for path in reversed(mounts):
        run('umount', path)
    for device in reversed(loops):
        run('losetup','-d',device)
    shutil.rmtree(root)
