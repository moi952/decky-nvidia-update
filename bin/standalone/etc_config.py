#!/usr/bin/python3 -I
"""Keep user configuration out of the immutable driver image.

Only deletions are reflected into the disposable clone. Additions and edits
remain in Valve's /etc upper layer and follow its normal migration policy.
This cannot reconstruct configuration baked into a base by older releases.
"""
import os
import hashlib
from pathlib import Path
import re
import shutil
import stat
import sys

MAX_DELETED_ENTRIES = 200


def mountpoints():
    # ismount alone misses bind mounts on the same filesystem.
    return {Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)),
                        line.split()[4]))
            for line in Path('/proc/self/mountinfo').read_text().splitlines()}


def directory(path):
    path = Path(path).absolute()
    # Never follow a symlink in either tree's root path.
    for part in reversed((path, *path.parents)):
        if not stat.S_ISDIR(part.lstat().st_mode):
            raise ValueError(f'Expected a real directory: {part}')
    return path


def entry(root, name):
    """Read a bounded entry without following symlinks in its parents."""
    path = root
    for component in Path(name).parts:
        path = path / component
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if path == root / name:
            return info
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f'Unsupported configuration directory: {path}')


def check_legacy(base):
    """Find only known local service entries in an older flattened base."""
    base = directory(base)
    names = ('systemd/system/plugin_loader.service',
             'systemd/system/sshd.service',
             'systemd/system/multi-user.target.wants/plugin_loader.service',
             'systemd/system/multi-user.target.wants/sshd.service')
    found = []
    for name in names:
        info = entry(base, name)
        if info is not None:
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError(f'Unsupported legacy service type: {name}')
            found.append(name)
    return found


def signature(path):
    info = path.lstat()
    if stat.S_ISREG(info.st_mode):
        if info.st_size > 1024 * 1024 or info.st_nlink != 1:
            raise ValueError('Unexpected size or hard links in legacy service entry')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_dev, opened.st_size) != (info.st_ino, info.st_dev, info.st_size):
                raise ValueError('Legacy service changed while reading')
            data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError('Legacy service grew while reading')
            content = hashlib.sha256(data).hexdigest()
    elif stat.S_ISLNK(info.st_mode):
        content = os.readlink(path)
    else:
        raise ValueError('Unsupported legacy service entry type')
    attrs = {name: os.getxattr(path, name, follow_symlinks=False)
             for name in os.listxattr(path, follow_symlinks=False)
             if not name.startswith(('trusted.overlay.', 'user.overlay.'))}
    return stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, content, attrs


def require_overlay(live, upper):
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        decode = lambda value: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
        if Path(decode(fields[4])) != live:
            continue
        start = fields.index('-')
        options = dict(part.split('=', 1) for part in fields[start + 3].split(',') if '=' in part)
        allowed = {str(upper)}
        # SteamOS mounts /etc before switch_root. Overlay mount options retain
        # their initramfs spelling after /new_root becomes /. Accept only this
        # observed, complete layout, not arbitrary suffix/path-prefix matches.
        if (live == Path('/etc') and upper == Path('/var/lib/overlays/etc/upper')
                and decode(options.get('lowerdir', '')) == '/new_root/etc'
                and decode(options.get('workdir', '')) == '/new_root/var/lib/overlays/etc/work'):
            allowed.add('/new_root/var/lib/overlays/etc/upper')
        if (fields[start + 1] == 'overlay' and fields[3] == '/'
                and 'rw' in fields[5].split(',') and decode(options.get('upperdir', '')) in allowed):
            return
    raise ValueError('Live /etc is not the expected writable overlay; legacy migration stopped')


def force_copy_up(path, upper_path):
    """Ask overlayfs to copy an inode without changing its effective contents."""
    before = signature(path)
    if before[0] == stat.S_IFREG:
        # Opening for write requests data copy-up even with metacopy enabled.
        # Do not write or replace the file: its contents and metadata stay intact.
        os.close(os.open(path, os.O_RDWR | os.O_NOFOLLOW))
    else:
        os.chown(path, before[2], before[3], follow_symlinks=False)
    if signature(path) != before or signature(upper_path) != before:
        raise ValueError('Legacy copy-up verification failed; no slot will be written')
    if any(name.endswith('.metacopy') for name in os.listxattr(upper_path, follow_symlinks=False)):
        raise ValueError('Legacy copy-up did not materialize the complete file')


def migrate_legacy(live, base, target, upper):
    """Move recognized service settings to upper, preserving current enablement.

Only the disposable base loses entries. The source base is never written. A
disabled/missing live entry stays absent; a /dev/null mask stays a mask. Other
previously flattened settings require separate review against a stock baseline.
"""
    live, base, target, upper = map(directory, (live, base, target, upper))
    roots = (live, base, target, upper)
    if any(a == b or a in b.parents or b in a.parents
           for index, a in enumerate(roots) for b in roots[index + 1:]):
        raise ValueError('Legacy migration trees must be separate')
    names = check_legacy(base)
    if not names:
        return
    require_overlay(live, upper)
    mounts = mountpoints()
    if any(target in path.parents or upper in path.parents for path in mounts):
        raise ValueError('Unexpected mount in legacy migration destination')
    plan = []
    # Validate every path before even a metadata-only operation on live /etc.
    for name in names:
        entry(target, name)
        if signature(base / name) != signature(target / name):
            raise ValueError('Cloned legacy service differs from the source base')
        current = entry(live, name)
        if any(point == live / name or point in (live / name).parents
               for point in mounts if point != live and live in point.parents):
            raise ValueError('Legacy service is on a nested live mount')
        upper_info = entry(upper, name)
        if upper_info is not None and not (stat.S_ISREG(upper_info.st_mode) or
                                          stat.S_ISLNK(upper_info.st_mode) or
                                          (stat.S_ISCHR(upper_info.st_mode) and upper_info.st_rdev == 0)):
            raise ValueError('Unsupported legacy upper entry')
        value = signature(live / name) if current is not None else None
        plan.append((name, value))
    for name, value in plan:
        if value is not None:
            if signature(live / name) != value:
                raise ValueError('Service settings changed during preparation; retry')
            force_copy_up(live / name, upper / name)
    # Verify the whole set before removing anything from the disposable clone.
    for name, value in plan:
        if value is None:
            if entry(live, name) is not None:
                raise ValueError('Service enablement changed during preparation; retry')
        elif signature(live / name) != value or signature(upper / name) != value:
            raise ValueError('Service settings changed during preparation; retry')
    for name, _ in plan:
        (target / name).unlink()
        print(f'[etc] Preserved current service state in overlay: {name}', flush=True)
    print('[etc] Older flattened configuration detected. Service migration is limited to '
          'Decky and SSH service state; other baked settings, passwords and SSH host keys '
          'need separate review before the next OS update.', flush=True)


def prune_deleted(live, target):
    """Remove only clone entries absent from live /etc, without following links.

Collect and validate the complete plan before deleting anything, so a missing
or unreadable source tree cannot turn into an empty-source mirror operation.
The caller supplies a private, freshly cloned and not yet written root image.
"""
    live, target = directory(live), directory(target)
    if live == target or live in target.parents or target in live.parents:
        raise ValueError('Configuration trees must be separate')
    mounts = mountpoints()
    if any(target in point.parents for point in mounts):
        raise ValueError('Unexpected nested mount in cloned configuration')
    deletions = []
    removed_entries = 0

    def count_tree(path):
        count = 1
        if stat.S_ISDIR(path.lstat().st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    count += count_tree(path / entry.name)
                    if count > MAX_DELETED_ENTRIES:
                        raise ValueError('Too many deleted configuration entries; review /etc before retrying')
        return count

    def scan(source, destination):
        nonlocal removed_entries
        with os.scandir(source) as entries:
            current = {entry.name: entry.stat(follow_symlinks=False) for entry in entries}
        with os.scandir(destination) as entries:
            for entry in entries:
                path = destination / entry.name
                # Bind-mounted configuration is not an overlay deletion. Do not
                # inspect its contents or prune the hidden base beneath it.
                if source / entry.name in mounts:
                    continue
                info = entry.stat(follow_symlinks=False)
                other = current.get(entry.name)
                if other is None:
                    removed_entries += count_tree(path)
                    if removed_entries > MAX_DELETED_ENTRIES:
                        raise ValueError('Too many deleted configuration entries; review /etc before retrying')
                    deletions.append((path, stat.S_ISDIR(info.st_mode)))
                elif stat.S_ISDIR(info.st_mode) and stat.S_ISDIR(other.st_mode):
                    scan(source / entry.name, path)

    scan(live, target)
    for path, is_directory in deletions:
        if is_directory:
            shutil.rmtree(path)
        else:
            path.unlink()
        print(f'[etc] Removed cloned base entry: {str(path.relative_to(target))!r}', flush=True)


if __name__ == '__main__':
    action, *args = sys.argv[1:]
    if action == 'check-legacy' and len(args) == 1:
        check_legacy(Path(args[0]))
    elif action == 'migrate-legacy' and len(args) == 4:
        migrate_legacy(*map(Path, args))
    elif action == 'prune-deleted' and len(args) == 2:
        prune_deleted(*map(Path, args))
    else:
        raise SystemExit('Expected check-legacy BASE_ETC, migrate-legacy LIVE BASE TARGET UPPER, or prune-deleted LIVE_ETC TARGET_ETC')
