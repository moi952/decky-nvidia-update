"""User /etc changes must survive a driver change followed by a SteamOS update.

SteamOS keeps user changes to /etc in an overlay upper layer. On an update, Valve's
holo-sync-var copies only that upper layer into the new slot, and it skips every
file that is identical to the new slot's base (--compare-dest). Staging once
copied the live, merged /etc into the new base. Decky's service, the SSH
autostart and the password then lived only in the base, and the next SteamOS
update replaced the base and lost them.

These tests replay that lifecycle on plain directories with Valve's own rsync
options and keep list, against the real staging helpers.
"""
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = ROOT / 'bin/standalone'

# /usr/lib/rauc/atomic-update-keep.conf from SteamOS 3.9.5 (sha256 acebcc83...,
# unchanged from 3.8.28), comments and blank lines removed.
VALVE_KEEP = """\
/etc/atomic-update.conf.d/*.conf
/etc/dconf/**
/etc/NetworkManager/system-connections/*
/etc/sddm.conf.d/*
/etc/ssh/*_key
/etc/ssh/*_key.pub
/etc/wireguard/*.conf
/etc/steamos-atomupd/preferences.conf
/etc/systemd/system/*.requires/**
/etc/systemd/system/*.wants/**
/etc/systemd/system/*.service
/etc/systemd/system/*.service.d/**
/etc/systemd/system/*.socket
/etc/systemd/system/*.socket.d/**
/etc/systemd/system/*.mount
/etc/systemd/system/*.mount.d/**
/etc/.devkit-service-on-os-update
/etc/hostname
/etc/localtime
/etc/machine-id
/etc/resolv.conf
/etc/group
/etc/gshadow
/etc/passwd
/etc/shadow
/etc/subgid
/etc/subuid
/etc/environment
"""

# The staging line released up to 0.4.0-standalone.9 (stage.sh:46 at 075854d).
FLATTENING_STAGE = 'rsync -aHAXx --delete --numeric-ids "$LIVE_ETC/" "$WORK_ROOT_ETC/"'

DECKY_UNIT = 'systemd/system/plugin_loader.service'
DECKY_LINK = 'systemd/system/multi-user.target.wants/plugin_loader.service'
SSHD_LINK = 'systemd/system/multi-user.target.wants/sshd.service'


def load_etc_config():
    spec = importlib.util.spec_from_file_location('etc_config', STANDALONE / 'etc_config.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def link(root, name, target):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return path


def is_whiteout(info):
    return stat.S_ISCHR(info.st_mode) and info.st_rdev == 0


def snapshot(root):
    """Map every path under root to its kind and content, without following links."""
    result = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[relative] = ('link', os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                result[relative] = ('dir', None)
            elif is_whiteout(info):
                result[relative] = ('whiteout', None)
            else:
                result[relative] = ('file', path.read_bytes())
    return result


def merged(base, upper):
    """The /etc a booted slot shows: upper over base, whiteouts hiding base entries."""
    view = {k: v for k, v in snapshot(base).items()}
    for name, entry in sorted(snapshot(upper).items()):
        if entry[0] == 'whiteout':
            view = {k: v for k, v in view.items() if k != name and not k.startswith(name + '/')}
        elif entry[0] == 'dir' and view.get(name, ('dir',))[0] == 'dir':
            view[name] = entry
        else:
            view = {k: v for k, v in view.items() if not k.startswith(name + '/')}
            view[name] = entry
    return view


def materialize(view, destination):
    """Write a merged view to disk, standing in for the running system's /etc."""
    for name, (kind, value) in sorted(view.items()):
        path = destination / name
        if kind == 'dir':
            path.mkdir(parents=True, exist_ok=True)
        elif kind == 'link':
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(value)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)


def valve_migrate(old_upper, new_upper, new_base_etc, scratch):
    """holo-sync-var's rsync_etc_overlay, with the update's new base as --compare-dest."""
    config = scratch / 'etc-keep.conf'
    config.write_text(''.join(line.replace('/etc', '', 1) + '\n' for line in VALVE_KEEP.splitlines()))
    new_upper.mkdir(parents=True, exist_ok=True)
    options = ['-rlpgoDH', '--delete', '--one-file-system', '--checksum', '--prune-empty-dirs',
               f'--compare-dest={new_base_etc}', '--include=*/', f'--include-from={config}', '--exclude=*']
    result = subprocess.run(['rsync', *options, f'{old_upper}/', f'{new_upper}/'],
                            capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)


def pristine_base(root, release):
    """A minimal Valve base /etc: no Decky, SSH not enabled, the image's own password file."""
    write(root, 'os-release', f'VERSION_ID={release}\n')
    write(root, 'passwd', 'root:x:0:0::/root:/bin/bash\ndeck:x:1000:1000::/home/deck:/bin/bash\n')
    write(root, 'shadow', 'root:!*:1::::::\ndeck:!*:1::::::\n')
    write(root, 'pacman.conf', '[jupiter-3.8]\nInclude = /etc/pacman.d/mirrorlist\n')
    write(root, 'pacman.d/mirrorlist', 'Server = https://example.invalid/$repo/os/$arch\n')
    link(root, 'systemd/system/multi-user.target.wants/NetworkManager.service',
         '/usr/lib/systemd/system/NetworkManager.service')
    write(root, 'systemd/system/holo-post-update-shutdown.service.d/60-efi-mounts.conf', '[Unit]\n')


def user_changes(upper):
    """What the owner had done on the running system before the driver change."""
    write(upper, DECKY_UNIT, '[Service]\nExecStart=/home/deck/homebrew/services/PluginLoader\n')
    link(upper, DECKY_LINK, '/etc/systemd/system/plugin_loader.service')
    link(upper, SSHD_LINK, '/usr/lib/systemd/system/sshd.service')
    write(upper, 'shadow', 'root:!*:1::::::\ndeck:$6$owner$secret:20000::::::\n')


@unittest.skipUnless(os.name == 'posix' and shutil.which('rsync'), 'Requires Linux and rsync')
class DriverChangeThenSteamOSUpdate(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.source_base = self.tmp / 'slot-a/base/etc'
        self.upper = self.tmp / 'slot-a/upper'
        self.live = self.tmp / 'slot-a/live-etc'
        pristine_base(self.source_base, '3.8.28')
        self.upper.mkdir(parents=True)
        user_changes(self.upper)
        materialize(merged(self.source_base, self.upper), self.live)

    def stage(self, flatten):
        """The driver change: clone the base, then either the old flatten or the new helper."""
        target = self.tmp / 'slot-b/base/etc'
        shutil.copytree(self.source_base, target, symlinks=True)
        if flatten:
            result = subprocess.run(['bash', '-eu', '-c', FLATTENING_STAGE],
                                    env={**os.environ, 'LIVE_ETC': str(self.live), 'WORK_ROOT_ETC': str(target)},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            load_etc_config().prune_deleted(self.live, target)
        # The payload's deliberate base changes (common.sh).
        write(target, 'modprobe.d/99-nvidia-patch.conf', 'blacklist nouveau\n')
        link(target, 'systemd/system/multi-user.target.wants/nvidia-suspend.service',
             '/usr/lib/systemd/system/nvidia-suspend.service')
        new_upper = self.tmp / 'slot-b/upper'
        valve_migrate(self.upper, new_upper, target, self.tmp)
        return target, new_upper

    def steamos_update(self, upper):
        """A real SteamOS update: Valve's fresh base, upper migrated against it."""
        base = self.tmp / 'slot-a-new/base/etc'
        pristine_base(base, '3.9.5')
        new_upper = self.tmp / 'slot-a-new/upper'
        valve_migrate(upper, new_upper, base, self.tmp)
        return base, new_upper

    def assert_owner_settings(self, view, present):
        for name in (DECKY_UNIT, DECKY_LINK, SSHD_LINK):
            self.assertEqual(name in view, present, name)
        self.assertEqual(b'$6$owner$secret' in view['shadow'][1], present)

    def test_fixed_staging_keeps_settings_out_of_the_base(self):
        base, upper = self.stage(flatten=False)
        for name in (DECKY_UNIT, DECKY_LINK, SSHD_LINK):
            self.assertFalse(os.path.lexists(base / name), f'{name} was baked into the base')
        self.assertNotIn(b'$6$owner', (base / 'shadow').read_bytes())
        self.assert_owner_settings(merged(base, upper), present=True)

    def test_fixed_staging_settings_survive_the_next_steamos_update(self):
        _, upper = self.stage(flatten=False)
        base, upper = self.steamos_update(upper)
        view = merged(base, upper)
        self.assert_owner_settings(view, present=True)
        self.assertEqual(view['os-release'][1], b'VERSION_ID=3.9.5\n')

    def test_released_flattening_reproduces_the_loss(self):
        # The scenario must be able to fail: the released staging line loses all three.
        _, upper = self.stage(flatten=True)
        base, upper = self.steamos_update(upper)
        self.assert_owner_settings(merged(base, upper), present=False)

    def test_deliberate_driver_changes_stay_in_the_base(self):
        base, upper = self.stage(flatten=False)
        self.assertTrue((base / 'modprobe.d/99-nvidia-patch.conf').is_file())
        self.assertTrue((base / 'systemd/system/multi-user.target.wants/nvidia-suspend.service').is_symlink())
        self.assertNotIn('modprobe.d/99-nvidia-patch.conf', snapshot(upper))

    def test_live_system_is_not_modified(self):
        before = snapshot(self.live)
        self.stage(flatten=False)
        self.assertEqual(snapshot(self.live), before)


@unittest.skipUnless(os.name == 'posix' and shutil.which('rsync'), 'Requires Linux and rsync')
class LegacyFlattenedBase(unittest.TestCase):
    """A slot prepared by the released staging: Decky and SSH live only in its base."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.base = self.tmp / 'slot-a/base/etc'
        self.upper = self.tmp / 'slot-a/upper'
        self.live = self.tmp / 'slot-a/live-etc'
        pristine_base(self.base, '3.8.28')
        user_changes(self.base)
        # The released migration dropped every upper entry identical to that base.
        self.upper.mkdir(parents=True)
        materialize(merged(self.base, self.upper), self.live)
        self.helper = load_etc_config()
        self.helper.require_overlay = lambda live, upper: None
        self.helper.mountpoints = lambda: set()

        def copy_up(path, upper_path):
            # Overlayfs copy-up: the same entry appears in the upper layer, unchanged.
            upper_path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                upper_path.symlink_to(os.readlink(path))
            else:
                shutil.copy2(path, upper_path)

        self.helper.force_copy_up = copy_up

    def stage_then_update(self, migrate):
        target = self.tmp / 'slot-b/base/etc'
        shutil.copytree(self.base, target, symlinks=True)
        if migrate:
            self.helper.migrate_legacy(self.live, self.base, target, self.upper)
        self.helper.prune_deleted(self.live, target)
        new_upper = self.tmp / 'slot-b/upper'
        valve_migrate(self.upper, new_upper, target, self.tmp)
        updated = self.tmp / 'slot-b-new/base/etc'
        pristine_base(updated, '3.9.5')
        updated_upper = self.tmp / 'slot-b-new/upper'
        valve_migrate(new_upper, updated_upper, updated, self.tmp)
        return merged(updated, updated_upper)

    def test_service_state_survives_after_migration(self):
        view = self.stage_then_update(migrate=True)
        for name in (DECKY_UNIT, DECKY_LINK, SSHD_LINK):
            self.assertIn(name, view)

    def test_without_migration_the_legacy_base_still_loses_it(self):
        view = self.stage_then_update(migrate=False)
        for name in (DECKY_UNIT, DECKY_LINK, SSHD_LINK):
            self.assertNotIn(name, view)

    def test_baked_password_is_a_documented_limit(self):
        # Only service state is migrated. A password baked into an old base is not recovered.
        view = self.stage_then_update(migrate=True)
        self.assertNotIn(b'$6$owner$secret', view['shadow'][1])


@unittest.skipUnless(os.name == 'posix' and shutil.which('rsync') and os.geteuid() == 0,
                     'Whiteouts need root (mknod)')
class DeletedBaseConfiguration(unittest.TestCase):
    """A base file the user removed is a whiteout in the upper layer."""

    def test_deletion_outside_the_keep_list_does_not_return_after_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source, upper, live = tmp / 'base/etc', tmp / 'upper', tmp / 'live'
            pristine_base(source, '3.8.28')
            # sysctl.d is not on Valve's keep list, so the whiteout itself is not migrated.
            removed = 'sysctl.d/50-valve.conf'
            write(source, removed, 'vm.example = 1\n')
            (upper / removed).parent.mkdir(parents=True)
            try:
                os.mknod(upper / removed, stat.S_IFCHR | 0o000, os.makedev(0, 0))
            except PermissionError:
                self.skipTest('This environment does not allow device nodes')
            materialize(merged(source, upper), live)
            self.assertNotIn(removed, merged(source, upper))
            target = tmp / 'target/etc'
            shutil.copytree(source, target, symlinks=True)
            unpruned = tmp / 'unpruned/etc'
            shutil.copytree(source, unpruned, symlinks=True)
            load_etc_config().prune_deleted(live, target)
            new_upper = tmp / 'new-upper'
            valve_migrate(upper, new_upper, target, tmp)
            self.assertNotIn(removed, merged(target, new_upper))
            # Without the prune the removed file would come back after staging.
            control_upper = tmp / 'control-upper'
            valve_migrate(upper, control_upper, unpruned, tmp)
            self.assertIn(removed, merged(unpruned, control_upper))


class StagingScripts(unittest.TestCase):
    """The shell steps that decide what reaches the immutable base."""

    def setUp(self):
        self.stage = (STANDALONE / 'stage.sh').read_text()
        self.payload = (STANDALONE / 'payload.sh').read_text()

    def code(self, text):
        return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]

    def test_no_step_copies_the_live_etc_into_the_new_root(self):
        for line in self.code(self.stage):
            if re.match(r'(rsync|cp)\b', line):
                self.assertFalse(re.search(r'\s/etc/?\.?["\s]', line + ' ') and '$WORK/root' in line, line)

    def test_staging_order(self):
        lines = self.code(self.stage)

        def index(pattern):
            found = [i for i, line in enumerate(lines) if re.search(pattern, line)]
            self.assertEqual(len(found), 1, pattern)
            return found[0]

        sanity = index(r'-f /etc/passwd && -e /etc/os-release')
        legacy = index(r'etc_config\.py" check-legacy "\$WORK/source/etc"')
        write_root = index(r'^mount -o compress-force')
        clone = index(r'^rsync -aHAXx --numeric-ids "\$WORK/source/" "\$WORK/root/"')
        migrate = index(r'etc_config\.py" migrate-legacy /etc "\$WORK/source/etc" "\$WORK/root/etc" '
                        r'/var/lib/overlays/etc/upper$')
        prune = index(r'etc_config\.py" prune-deleted /etc "\$WORK/root/etc"')
        build = index(r'^build_payload ')
        self.assertLess(sanity, legacy)
        self.assertLess(legacy, write_root)
        self.assertLess(clone, migrate)
        self.assertLess(migrate, prune)
        self.assertLess(prune, build)
        self.assertIn('--build-etc /etc', lines[build])

    def test_build_configuration_stays_in_the_disposable_overlay(self):
        lines = self.code(self.payload)
        mount = next(i for i, line in enumerate(lines) if line.startswith('mount -t overlay'))
        first_pacman = next(i for i, line in enumerate(lines) if re.search(r'in_chroot "pacman[ -]', line))
        copies = [i for i, line in enumerate(lines)
                  if line.startswith(('cp ', 'rsync ', '"${PACMAN_COPY[@]}" ')) and re.search(r'"\$BUILD_ETC/pacman\.(conf|d/)"', line)]
        self.assertTrue(copies)
        for i in copies:
            self.assertLess(mount, i)
            self.assertLess(i, first_pacman)
            self.assertIn('"$MERGED/etc/', lines[i])
        self.assertIn('BUILD_ETC=${BUILD_ETC:-"$TARGET/etc"}', lines)
        for line in lines:
            if line.startswith(('JUPITER_REPO=', 'MIRROR=')):
                self.assertIn('$MERGED/etc/', line)

    def test_pacman_configuration_cannot_leave_through_the_file_list(self):
        guard = next(line for line in self.code(self.payload) if line.startswith('if grep -Eq') and 'pacman' in line)
        pattern = re.search(r"'([^']+)'", guard).group(1)
        for name in ('etc/pacman.conf', 'etc/pacman.d', 'etc/pacman.d/', 'etc/pacman.d/mirrorlist',
                     'etc/pacman.d/gnupg/pubring.gpg'):
            self.assertTrue(re.search(pattern, name), name)
        for name in ('etc/pacman.conf.d.bak/x', 'usr/share/pacman/keyrings/archlinux.gpg', 'etc/dkms/framework.conf',
                     'etc/pacmanrc'):
            self.assertFalse(re.search(pattern, name), name)
        rsync = next(i for i, line in enumerate(self.code(self.payload)) if '--files-from="$FILELIST.rel"' in line)
        self.assertLess(self.code(self.payload).index(guard), rsync)


if __name__ == '__main__':
    unittest.main()
