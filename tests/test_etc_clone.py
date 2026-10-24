"""The /etc helpers used by staging: prune deletions, refuse a baked legacy base."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'bin/standalone/etc_config.py'
spec = importlib.util.spec_from_file_location('etc_config', HELPER)
etc_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(etc_config)


def tree(root):
    """Every path under root with its kind and content, without following links."""
    result = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ('link', os.readlink(path))
            elif path.is_dir():
                result[relative] = ('dir', None)
            else:
                result[relative] = ('file', path.read_bytes())
    return result


@unittest.skipUnless(os.name == 'posix', 'Requires Linux')
class PruneDeleted(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.live = self.base / 'live'
        self.target = self.base / 'work/root/etc'
        self.live.mkdir()
        self.target.mkdir(parents=True)

    def test_deleted_base_configuration_does_not_return_in_clone(self):
        (self.live / 'kept.conf').write_text('current setting\n')
        (self.target / 'kept.conf').write_text('old setting\n')
        (self.target / 'deleted.conf').write_text('removed by the user\n')
        (self.target / 'removed.d').mkdir()
        (self.target / 'removed.d/stale.conf').write_text('obsolete\n')
        outside = self.base / 'outside.conf'
        outside.write_text('must remain unchanged\n')
        (self.live / 'linked.conf').symlink_to(outside)
        (self.live / 'added.conf').write_text('live only\n')
        before = tree(self.live)
        etc_config.prune_deleted(self.live, self.target)
        self.assertEqual(sorted(tree(self.target)), ['kept.conf'])
        # Only deletions are reflected: edits and additions stay in the overlay.
        self.assertEqual((self.target / 'kept.conf').read_text(), 'old setting\n')
        self.assertEqual(outside.read_text(), 'must remain unchanged\n')
        self.assertEqual(tree(self.live), before)

    def test_nested_deletions_are_pruned_and_present_directories_kept(self):
        (self.live / 'systemd/system').mkdir(parents=True)
        (self.live / 'systemd/system/kept.service').write_text('[Unit]\n')
        (self.target / 'systemd/system/gone.service.d').mkdir(parents=True)
        (self.target / 'systemd/system/gone.service.d/10.conf').write_text('[Unit]\n')
        (self.target / 'systemd/system/kept.service').write_text('[Unit]\n')
        etc_config.prune_deleted(self.live, self.target)
        self.assertEqual(sorted(tree(self.target)), ['systemd', 'systemd/system', 'systemd/system/kept.service'])

    def test_type_changes_are_not_deletions(self):
        (self.target / 'was-dir').mkdir()
        (self.target / 'was-dir/inner.conf').write_text('base\n')
        (self.live / 'was-dir').write_text('now a file\n')
        (self.target / 'was-file').write_text('base\n')
        (self.live / 'was-file').mkdir()
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        (self.target / 'now-link').mkdir()
        (self.target / 'now-link/inner.conf').write_text('base\n')
        (self.live / 'now-link').symlink_to(elsewhere)
        etc_config.prune_deleted(self.live, self.target)
        self.assertEqual((self.target / 'was-dir/inner.conf').read_text(), 'base\n')
        self.assertEqual((self.target / 'was-file').read_text(), 'base\n')
        # A live symlink is not descended into, so nothing under the base directory is pruned.
        self.assertEqual((self.target / 'now-link/inner.conf').read_text(), 'base\n')

    def test_symlinks_in_the_clone_are_removed_not_followed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'precious.conf').write_text('keep\n')
        (self.target / 'dir-link').symlink_to(outside)
        (self.target / 'file-link').symlink_to(outside / 'precious.conf')
        (self.target / 'dangling').symlink_to(self.base / 'missing')
        etc_config.prune_deleted(self.live, self.target)
        self.assertEqual(tree(self.target), {})
        self.assertEqual((outside / 'precious.conf').read_text(), 'keep\n')

    def test_missing_live_tree_fails_without_deleting(self):
        retained = self.target / 'retained.conf'
        retained.write_text('preserve on source failure\n')
        with self.assertRaises((OSError, ValueError)):
            etc_config.prune_deleted(self.base / 'missing-source', self.target)
        self.assertEqual(retained.read_text(), 'preserve on source failure\n')

    def test_symlinked_roots_are_refused(self):
        (self.target / 'retained.conf').write_text('x\n')
        live_link = self.base / 'live-link'
        live_link.symlink_to(self.live)
        target_link = self.base / 'target-link'
        target_link.symlink_to(self.target)
        with self.assertRaises(ValueError):
            etc_config.prune_deleted(live_link, self.target)
        with self.assertRaises(ValueError):
            etc_config.prune_deleted(self.live, target_link)
        self.assertTrue((self.target / 'retained.conf').exists())

    def test_overlapping_trees_are_refused(self):
        (self.live / 'inner').mkdir()
        for live, target in ((self.live, self.live), (self.live, self.live / 'inner'), (self.live / 'inner', self.live)):
            with self.assertRaises(ValueError):
                etc_config.prune_deleted(live, target)
        self.assertTrue((self.live / 'inner').is_dir())

    @unittest.skipIf(os.name == 'posix' and os.geteuid() == 0, 'Root ignores directory permissions')
    def test_unreadable_live_directory_deletes_nothing(self):
        (self.target / 'absent.conf').write_text('would be pruned\n')
        (self.live / 'private').mkdir()
        (self.target / 'private').mkdir()
        (self.target / 'private/x.conf').write_text('base\n')
        os.chmod(self.live / 'private', 0)
        self.addCleanup(os.chmod, self.live / 'private', 0o755)
        with self.assertRaises(OSError):
            etc_config.prune_deleted(self.live, self.target)
        # The whole plan is collected before the first deletion.
        self.assertTrue((self.target / 'absent.conf').exists())
        self.assertTrue((self.target / 'private/x.conf').exists())

    def test_command_line(self):
        (self.target / 'gone.conf').write_text('x\n')
        result = subprocess.run([sys.executable, '-I', str(HELPER), 'prune-deleted', str(self.live), str(self.target)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.target / 'gone.conf').exists())
        result = subprocess.run([sys.executable, '-I', str(HELPER), 'prune-deleted', str(self.live)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


LEGACY = ('systemd/system/plugin_loader.service',
          'systemd/system/sshd.service',
          'systemd/system/multi-user.target.wants/plugin_loader.service',
          'systemd/system/multi-user.target.wants/sshd.service')


def legacy_entry(root, name, mask=False):
    """Create one of the recognised service entries the way systemctl and Decky do."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if mask:
        path.symlink_to('/dev/null')
    elif '.wants/' in name:
        path.symlink_to('/etc/systemd/system/plugin_loader.service' if 'plugin_loader' in name
                        else '/usr/lib/systemd/system/sshd.service')
    else:
        path.write_text('[Service]\nExecStart=/home/deck/homebrew/services/PluginLoader\n')
    return path


@unittest.skipUnless(os.name == 'posix', 'Requires Linux')
class CheckLegacy(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name) / 'etc'
        (self.base / 'systemd/system/multi-user.target.wants').mkdir(parents=True)
        (self.base / 'systemd/system/multi-user.target.wants/NetworkManager.service').symlink_to(
            '/usr/lib/systemd/system/NetworkManager.service')

    def test_clean_base_finds_nothing(self):
        self.assertEqual(etc_config.check_legacy(self.base), [])

    def test_each_recognised_entry_is_reported(self):
        for name in LEGACY:
            with self.subTest(name=name):
                path = legacy_entry(self.base, name)
                self.assertEqual(etc_config.check_legacy(self.base), [name])
                path.unlink()

    def test_all_entries_and_dangling_links_are_reported(self):
        for name in LEGACY:
            legacy_entry(self.base, name)
        (self.base / LEGACY[3]).unlink()
        (self.base / LEGACY[3]).symlink_to(self.base / 'missing')
        self.assertEqual(sorted(etc_config.check_legacy(self.base)), sorted(LEGACY))

    def test_unsupported_types_are_refused(self):
        (self.base / LEGACY[0]).mkdir()
        with self.assertRaises(ValueError):
            etc_config.check_legacy(self.base)
        other = self.base.parent / 'other'
        (other / 'systemd').mkdir(parents=True)
        (other / 'systemd/system').write_text('not a directory\n')
        with self.assertRaises(ValueError):
            etc_config.check_legacy(other)
        link = self.base.parent / 'etc-link'
        link.symlink_to(self.base)
        with self.assertRaises(ValueError):
            etc_config.check_legacy(link)

    def test_command_line_validates_without_refusing(self):
        legacy_entry(self.base, LEGACY[0])
        result = subprocess.run([sys.executable, '-I', str(HELPER), 'check-legacy', str(self.base)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


def load_isolated():
    """A private copy of the helper whose kernel-facing functions a test may replace."""
    module_spec = importlib.util.spec_from_file_location('etc_config_isolated', HELPER)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def copy_entry(source, destination):
    """What overlayfs copy-up leaves in the upper layer: the same inode type, content and mode."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, destination)


@unittest.skipUnless(os.name == 'posix', 'Requires Linux')
class MigrateLegacy(unittest.TestCase):
    """Unit tests with the overlay copy-up replaced; the kernel path is in the overlay harness."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tmp = Path(temp.name)
        self.live, self.base, self.target, self.upper = (tmp / 'live', tmp / 'base', tmp / 'target', tmp / 'upper')
        for root in (self.live, self.base, self.target, self.upper):
            root.mkdir()
        self.helper = load_isolated()
        self.overlay_checks = []
        self.copy_ups = []
        self.helper.require_overlay = lambda live, upper: self.overlay_checks.append((live, upper))
        self.helper.mountpoints = lambda: set()
        self.helper.force_copy_up = self.fake_copy_up

    def fake_copy_up(self, path, upper_path):
        self.copy_ups.append(path.relative_to(self.live).as_posix())
        copy_entry(path, upper_path)

    def legacy(self, names, live=None, mask=()):
        """A flattened base cloned into target; live shows the base unless told otherwise."""
        for name in names:
            for root in (self.base, self.target):
                legacy_entry(root, name, mask=name in mask)
        for name in (names if live is None else live):
            legacy_entry(self.live, name, mask=name in mask)

    def test_no_legacy_entries_is_a_no_op(self):
        before = tree(self.target)
        self.helper.migrate_legacy(self.live, self.base, self.target, self.upper)
        self.assertEqual(tree(self.target), before)
        self.assertEqual(self.overlay_checks, [])
        self.assertEqual(self.copy_ups, [])

    def test_enabled_services_move_from_the_clone_to_the_overlay(self):
        self.legacy(LEGACY)
        base_before, live_before = tree(self.base), tree(self.live)
        self.helper.migrate_legacy(self.live, self.base, self.target, self.upper)
        for name in LEGACY:
            self.assertFalse(os.path.lexists(self.target / name), name)
            self.assertEqual(tree(self.upper).get(name), tree(self.live).get(name), name)
        self.assertEqual(tree(self.base), base_before)
        self.assertEqual(tree(self.live), live_before)
        self.assertEqual(self.overlay_checks, [(self.live, self.upper)])

    def test_disabled_service_stays_disabled(self):
        # The base still enables SSH, but the user disabled it: the live /etc lacks the link.
        self.legacy([LEGACY[3]], live=[])
        self.helper.migrate_legacy(self.live, self.base, self.target, self.upper)
        self.assertFalse(os.path.lexists(self.target / LEGACY[3]))
        self.assertFalse(os.path.lexists(self.upper / LEGACY[3]))
        self.assertEqual(self.copy_ups, [])

    def test_mask_stays_a_mask(self):
        self.legacy([LEGACY[1]], mask=[LEGACY[1]])
        self.helper.migrate_legacy(self.live, self.base, self.target, self.upper)
        self.assertEqual(os.readlink(self.upper / LEGACY[1]), '/dev/null')
        self.assertFalse(os.path.lexists(self.target / LEGACY[1]))

    def test_clone_differing_from_the_base_stops_before_any_copy_up(self):
        self.legacy(LEGACY)
        (self.target / LEGACY[0]).write_text('[Service]\nExecStart=/somewhere/else\n')
        before = tree(self.target)
        with self.assertRaises(ValueError):
            self.helper.migrate_legacy(self.live, self.base, self.target, self.upper)
        self.assertEqual(self.copy_ups, [])
        self.assertEqual(tree(self.target), before)

    def test_unsupported_upper_entry_stops_before_any_copy_up(self):
        self.legacy(LEGACY)
        (self.upper / LEGACY[0]).mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.helper.migrate_legacy(self.live, self.base, self.target, self.upper)
        self.assertEqual(self.copy_ups, [])

    def test_change_during_preparation_removes_nothing_from_the_clone(self):
        self.legacy(LEGACY)

        def copy_up_then_edit(path, upper_path):
            self.fake_copy_up(path, upper_path)
            (self.live / LEGACY[0]).write_text('[Service]\nExecStart=/edited/meanwhile\n')

        self.helper.force_copy_up = copy_up_then_edit
        before = tree(self.target)
        with self.assertRaises(ValueError):
            self.helper.migrate_legacy(self.live, self.base, self.target, self.upper)
        self.assertEqual(tree(self.target), before)

    def test_failed_upper_verification_removes_nothing_from_the_clone(self):
        self.legacy(LEGACY)

        def wrong_copy_up(path, upper_path):
            self.fake_copy_up(path, upper_path)
            if upper_path.is_symlink():
                upper_path.unlink()
                upper_path.symlink_to('/elsewhere')
            else:
                upper_path.write_text('different\n')

        self.helper.force_copy_up = wrong_copy_up
        before = tree(self.target)
        with self.assertRaises(ValueError):
            self.helper.migrate_legacy(self.live, self.base, self.target, self.upper)
        self.assertEqual(tree(self.target), before)

    def test_overlapping_trees_are_refused(self):
        self.legacy(LEGACY)
        with self.assertRaises(ValueError):
            self.helper.migrate_legacy(self.live, self.base, self.live / 'systemd', self.upper)
        self.assertEqual(self.copy_ups, [])

    def test_command_line_requires_four_paths(self):
        result = subprocess.run([sys.executable, '-I', str(HELPER), 'migrate-legacy', str(self.live), str(self.base)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


class RequireOverlay(unittest.TestCase):
    """Which /etc mounts allow the legacy copy-up. Lines are real or derived from real ones."""

    # Read on the test PC, SteamOS 3.9.5, 2026-10-24 (initramfs spelling of the options).
    STEAMOS = ('35 31 0:30 / /etc rw,relatime shared:3 - overlay overlay '
               'rw,lowerdir=/new_root/etc,upperdir=/new_root/var/lib/overlays/etc/upper,'
               'workdir=/new_root/var/lib/overlays/etc/work,index=off')
    # Read on SteamOS 3.8 on 2026-09-23, before index=off appeared.
    STEAMOS_38 = ('33 29 0:28 / /etc rw,relatime shared:3 - overlay overlay '
                  'rw,lowerdir=/new_root/etc,upperdir=/new_root/var/lib/overlays/etc/upper,'
                  'workdir=/new_root/var/lib/overlays/etc/work')
    OTHER = '30 1 0:26 / / ro,relatime shared:1 - btrfs /dev/sda5 ro,ssd,space_cache=v2,subvolid=5,subvol=/'

    def check(self, *lines, live='/etc', upper='/var/lib/overlays/etc/upper'):
        helper = load_isolated()
        text = '\n'.join(lines) + '\n'
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if str(path) == '/proc/self/mountinfo':
                return text
            return original(path, *args, **kwargs)

        with unittest.mock.patch.object(Path, 'read_text', read_text):
            helper.require_overlay(Path(live), Path(upper))

    def refused(self, *lines, **kwargs):
        with self.assertRaises(ValueError):
            self.check(*lines, **kwargs)

    def test_steamos_initramfs_spelling_is_accepted(self):
        self.check(self.OTHER, self.STEAMOS)
        self.check(self.STEAMOS_38)

    def test_direct_upper_spelling_is_accepted(self):
        self.check(self.STEAMOS.replace('upperdir=/new_root/var', 'upperdir=/var'))

    def test_other_prefixes_and_layouts_are_refused(self):
        self.refused(self.STEAMOS.replace('upperdir=/new_root/', 'upperdir=/sysroot/'))
        self.refused(self.STEAMOS.replace('lowerdir=/new_root/etc', 'lowerdir=/new_root/other'))
        self.refused(self.STEAMOS.replace('workdir=/new_root/var/lib/overlays/etc/work', 'workdir=/new_root/tmp/work'))
        self.refused(self.STEAMOS.replace('upperdir=/new_root/var/lib/overlays/etc/upper',
                                          'upperdir=/new_root/var/lib/overlays/etc/upper2'))

    def test_read_only_non_overlay_and_bind_mounts_are_refused(self):
        self.refused(self.STEAMOS.replace(' rw,relatime shared:3', ' ro,relatime shared:3'))
        self.refused(self.STEAMOS.replace('- overlay overlay', '- ext4 /dev/sda7'))
        self.refused(self.STEAMOS.replace(' / /etc ', ' /etc /etc '))

    def test_only_the_etc_mount_counts(self):
        self.refused(self.OTHER)
        self.refused(self.STEAMOS.replace(' / /etc ', ' / /mnt/etc '))
        # The initramfs spelling is only trusted for the real /etc and Valve's upper path.
        self.refused(self.STEAMOS.replace(' / /etc ', ' / /tmp/etc '), live='/tmp/etc')
        self.refused(self.STEAMOS, upper='/tmp/upper')


@unittest.skipUnless(os.name == 'posix', 'Requires Linux')
class PruneGuards(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tmp = Path(temp.name)
        self.live, self.target = tmp / 'live', tmp / 'target'
        self.live.mkdir()
        self.target.mkdir()
        self.helper = load_isolated()

    def test_live_mountpoint_is_not_treated_as_deletions(self):
        (self.live / 'bound').mkdir()
        (self.target / 'bound').mkdir()
        (self.target / 'bound/base-only.conf').write_text('hidden by a bind mount\n')
        self.helper.mountpoints = lambda: {self.live / 'bound'}
        self.helper.prune_deleted(self.live, self.target)
        self.assertTrue((self.target / 'bound/base-only.conf').exists())

    def test_mount_inside_the_clone_is_refused(self):
        (self.target / 'gone.conf').write_text('x\n')
        self.helper.mountpoints = lambda: {self.target / 'sub'}
        with self.assertRaises(ValueError):
            self.helper.prune_deleted(self.live, self.target)
        self.assertTrue((self.target / 'gone.conf').exists())

    def test_too_many_deletions_delete_nothing(self):
        self.helper.mountpoints = lambda: set()
        for index in range(self.helper.MAX_DELETED_ENTRIES + 1):
            (self.target / f'{index}.conf').write_text('x\n')
        with self.assertRaises(ValueError):
            self.helper.prune_deleted(self.live, self.target)
        self.assertEqual(len(list(self.target.iterdir())), self.helper.MAX_DELETED_ENTRIES + 1)

    def test_a_deleted_directory_counts_its_contents(self):
        self.helper.mountpoints = lambda: set()
        (self.target / 'big.d').mkdir()
        for index in range(self.helper.MAX_DELETED_ENTRIES):
            (self.target / f'big.d/{index}.conf').write_text('x\n')
        with self.assertRaises(ValueError):
            self.helper.prune_deleted(self.live, self.target)
        self.assertEqual(len(list((self.target / 'big.d').iterdir())), self.helper.MAX_DELETED_ENTRIES)

    def test_deletions_up_to_the_limit_proceed(self):
        self.helper.mountpoints = lambda: set()
        for index in range(self.helper.MAX_DELETED_ENTRIES):
            (self.target / f'{index}.conf').write_text('x\n')
        self.helper.prune_deleted(self.live, self.target)
        self.assertEqual(list(self.target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
