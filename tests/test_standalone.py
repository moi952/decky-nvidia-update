import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('driver', ROOT / 'bin/standalone/driver.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
VALVE = '''#!/bin/bash
steamos-chroot --partset $UPDATED_SLOT -- steamos-finalize-install --no-kernel
(( ERR == 0 )) && steamos-bootconf --image $UPDATED_SLOT set-mode reboot
'''
VERSION = '610.57.04-1'
# Reported Valve main layout. This is a fixture, not the mislabelled attachment,
# which still contains the older Stable finalization sequence.
VALVE_MAIN = '''#!/bin/bash
declare -ar FINISH=({holo,steamos}-finalize-install steamos-boot-install)
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
fi
(( ERR == 0 )) && holo-bootconf --image $UPDATED_SLOT set-mode first-boot
if [ $ERR == 0 ]; then
    echo "$BOOTED_SLOT" > "$HOLO_BOOTED_SLOT_SYNC_TRIGGER"
fi
'''
URLS = ' '.join(f'https://archive.archlinux.org/packages/{p[0]}/{p}/{p}-{VERSION}-x86_64.pkg.tar.zst'
                for p in ('nvidia-utils', 'nvidia-open-dkms', 'lib32-nvidia-utils'))


class RootSizeCompatibility(unittest.TestCase):
    def check_size(self, gib, available_gib):
        size = gib * 1024**3
        answers = {
            ('steamos-bootconf', 'selected-image'): 'A',
            ('btrfs', 'property', 'get', '/', 'ro'): 'ro=true',
            ('findmnt', '-n', '-o', 'TARGET', '--mountpoint', '/home'): '/home',
            ('atomupd-manager', 'get-update-status'): 'idle',
            ('busctl', 'get-property', 'de.pengutronix.rauc', '/', 'de.pengutronix.rauc.Installer', 'Operation'): 's "idle"',
            ('blockdev', '--getsize64', '/dev/test-target'): str(size),
        }
        with patch.object(d, 'layout', return_value=('A', 'B', {'B/rootfs': '/dev/test-target'})), patch.object(d, 'run', side_effect=lambda *args: answers[args]), patch.object(d.shutil, 'which', return_value='/usr/bin/tool'), patch.object(Path, 'read_text', return_value='ID=steamos\n'), patch.object(d, 'hook_text'), patch.object(d.subprocess, 'run'), patch.object(d.shutil, 'disk_usage', return_value=SimpleNamespace(free=available_gib * 1024**3)):
            return d.preflight()

    def test_original_and_enlarged_roots_use_the_detected_target_size(self):
        for gib in (5, 8):
            with self.subTest(root_gib=gib):
                result = self.check_size(gib, gib + 12)
                self.assertEqual(result['target_bytes'], gib * 1024**3)
                self.assertEqual(result['required_bytes'], (gib + 12) * 1024**3)
                self.assertTrue(result['enough_space'])

    def test_eight_gib_root_still_checks_home_space(self):
        self.assertFalse(self.check_size(8, 19)['enough_space'])


class TransactionStatus(unittest.TestCase):
    def test_restart_depends_on_the_current_boot_selection(self):
        cases = [
            ('ready', 'A', 'B', True),
            ('ready', 'B', 'B', False),
            ('ready', 'A', 'A', False),
            ('rollback-selected', 'B', 'A', True),
            ('rollback-selected', 'A', 'A', False),
            ('rollback-selected', 'B', 'B', False),
            ('failed', 'A', 'B', False),
        ]
        for status, active, selected, pending in cases:
            with self.subTest(status=status, active=active, selected=selected), patch.object(d, 'read_state', return_value=dict(status=status, source='A', target='B')), patch.object(d, 'run', side_effect=[active, selected]):
                result = d.transaction_status()
            self.assertEqual(result['pending_reboot'], pending)
            self.assertEqual(result['selected_slot'], selected)

    def test_incomplete_transaction_never_requests_a_reboot(self):
        with patch.object(d, 'read_state', return_value=dict(status='ready')), patch.object(d, 'run', side_effect=['A', 'B']):
            self.assertFalse(d.transaction_status()['pending_reboot'])


class RootDevice(unittest.TestCase):
    FSID = '7c1d2c2b-46d3-41e7-b7ad-e58ecc100598'

    def registry(self, root, devices):
        directory = root / self.FSID / 'devices'
        directory.mkdir(parents=True)
        for name, device in devices.items():
            (directory / name).mkdir()
            (directory / name / 'dev').write_text(device + '\n')

    def test_btrfs_uses_backing_device_not_anonymous_mount_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.registry(root, {'sda4': '8:4'})
            with patch.object(d, 'BTRFS_SYSFS', root), patch.object(d, 'run', side_effect=['btrfs', self.FSID]) as run:
                self.assertEqual(d.mounted_root_device(), '8:4')
                self.assertNotIn('MAJ:MIN', str(run.call_args_list))

    def test_missing_or_multiple_devices_refused(self):
        for devices in ({}, {'sda4': '8:4', 'sdb4': '8:20'}):
            with self.subTest(devices=devices), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.registry(root, devices)
                with patch.object(d, 'BTRFS_SYSFS', root), patch.object(d, 'run', side_effect=['btrfs', self.FSID]):
                    with self.assertRaisesRegex(ValueError, 'exactly one'):
                        d.mounted_root_device()

    def test_unsupported_filesystem_and_invalid_uuid_refused(self):
        for values in (['ext4'], ['btrfs', '../devices']):
            with self.subTest(values=values), patch.object(d, 'run', side_effect=values):
                with self.assertRaises(ValueError):
                    d.mounted_root_device()


class ImageRelease(unittest.TestCase):
    FSID = RootDevice.FSID

    def test_detached_and_unregistered_image_passes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(d, 'BTRFS_SYSFS', Path(tmp)), patch.object(d, 'run', return_value=''):
            d.require_image_released('/private/root.img', self.FSID, timeout=0)

    def test_loop_or_registered_filesystem_blocks_write(self):
        for loop, registered in (('/dev/loop1: [backing image]', False), ('', True)):
            with self.subTest(loop=loop, registered=registered), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if registered:
                    (root / self.FSID).mkdir()
                with patch.object(d, 'BTRFS_SYSFS', root), patch.object(d, 'run', return_value=loop):
                    with self.assertRaisesRegex(ValueError, 'refusing to write'):
                        d.require_image_released('/private/root.img', self.FSID, timeout=0)

    def test_waits_for_deferred_loop_detach(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(d, 'BTRFS_SYSFS', Path(tmp)), patch.object(d, 'run', side_effect=['/dev/loop1', '']), patch.object(d.time, 'sleep') as sleep:
            d.require_image_released('/private/root.img', self.FSID)
            sleep.assert_called_once_with(0.1)

    def test_bad_uuid_rejected_before_system_calls(self):
        with patch.object(d, 'run') as run, self.assertRaises(ValueError):
            d.require_image_released('/private/root.img', '../devices')
        run.assert_not_called()


class Hooks(unittest.TestCase):
    def test_stable_fallback_chain_stays_before_hook(self):
        # The maintainer's complete attachment uses this older fallback chain.
        chain = r'''steamos-chroot --partset $UPDATED_SLOT -- steamos-finalize-install --no-kernel || \
    steamos-chroot --partset $UPDATED_SLOT -- steamos-boot-install --no-kernel || \
    err "Failed to install bootloaders"'''
        original = VALVE.replace(d.FINALIZE_DIRECT, chain)
        result = d.hook_text(original, '/bin/true', temporary=True)
        self.assertIn(chain, result)
        self.assertLess(result.index(chain) + len(chain), result.index(d.HOOK_START))
        self.assertLess(result.index(d.HOOK_END), result.index('set-mode reboot'))

    @unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Linux Bash execution test')
    def test_main_execution_fallback_and_failures_never_activate_early(self):
        preamble = r'''
ERR=0 UPDATED_SLOT=B BOOTED_SLOT=A HOLO_BOOTED_SLOT_SYNC_TRIGGER=/dev/null
err() { ERR=1; printf 'finalize-error\n'; }
fail() { printf 'repair-error\n'; exit 42; }
sync() { :; }
holo-chroot() {
    printf 'finalize:%s\n' "$*"
    [[ "$FINALIZE_OK" == 1 && "$4" == steamos-finalize-install ]]
}
holo-bootconf() { printf 'bootconf:%s\n' "$*"; }
repair() { printf 'repair\n'; [[ "$REPAIR_OK" == 1 ]]; }
'''
        script = preamble + d.hook_text(VALVE_MAIN, 'repair', temporary=True) + '\nexit "$ERR"\n'
        for finalize_ok, repair_ok in ((True, True), (False, True), (True, False)):
            with self.subTest(finalize_ok=finalize_ok, repair_ok=repair_ok):
                env = dict(os.environ, FINALIZE_OK=str(int(finalize_ok)), REPAIR_OK=str(int(repair_ok)))
                result = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
                lines = result.stdout.splitlines()
                activation = [line for line in lines if 'set-mode first-boot' in line]
                self.assertEqual(bool(activation), finalize_ok and repair_ok, result.stdout + result.stderr)
                self.assertEqual('repair' in lines, finalize_ok)
                if finalize_ok and repair_ok:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertLess(next(i for i, line in enumerate(lines) if 'steamos-finalize-install' in line),
                                    lines.index('repair'))
                    self.assertLess(lines.index('repair'), lines.index(activation[0]))
                else:
                    self.assertNotEqual(result.returncode, 0)

    def test_main_loop_hook_follows_complete_finalization_and_preserves_tail(self):
        for namespace in ('holo', 'steamos'):
            original = VALVE_MAIN.replace('if holo-chroot ', f'if {namespace}-chroot ')
            result = d.hook_text(original, '/bin/true', temporary=True)
            self.assertLess(result.index('err "Failed to install bootloaders'), result.index(d.HOOK_START))
            self.assertLess(result.index(d.HOOK_END), result.index('set-mode first-boot'))
            self.assertIn('    holo-bootconf --image "$UPDATED_SLOT" config', result)
            self.assertEqual(result[result.index('(( ERR == 0 )) && holo-bootconf'):],
                             original[original.index('(( ERR == 0 )) && holo-bootconf'):])
            self.assertEqual(d.hook_text(result, '/bin/true', temporary=True), result)

    def test_main_loop_incomplete_ambiguous_or_wrong_order_is_rejected(self):
        for text in (VALVE_MAIN.replace('FINISHED=1', 'FINISHED=0'),
                     VALVE_MAIN.replace('        break\n', ''),
                     VALVE_MAIN.replace('if [ $FINISHED -ne 1 ]', 'if false'),
                     VALVE_MAIN + d.FINALIZE_DIRECT,
                     VALVE_MAIN + d.FINALIZE_LOOP,
                     VALVE_MAIN + d.ANCHORS[0],
                     VALVE_MAIN.replace(d.ANCHORS[-1] + '\n', '').replace(
                         '#!/bin/bash\n', '#!/bin/bash\n' + d.ANCHORS[-1] + '\n')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                d.hook_text(text, '/bin/true', temporary=True)

    def test_main_native_hook_is_preserved_or_replaced_only_temporarily(self):
        native = VALVE_MAIN.replace(d.ANCHORS[-1], d.NATIVE_START + '/native/repair other\n' + d.NATIVE_END + d.ANCHORS[-1])
        self.assertEqual(d.hook_text(native, '/standalone'), native)
        temporary = d.hook_text(native, '/standalone', temporary=True)
        self.assertNotIn('/native/repair', temporary)
        self.assertIn('/standalone', temporary)
        self.assertIn('HOLO_BOOTED_SLOT_SYNC_TRIGGER', temporary)

    def test_standalone_hook_is_before_activation_and_idempotent(self):
        result = d.hook_text(VALVE, '/bin/bash /usr/lib/decky-nvidia-update/repair.sh "$UPDATED_SLOT"')
        self.assertLess(result.index('NVIDIA preparation failed'), result.index('set-mode reboot'))
        self.assertIn('exec /usr/bin/unshare', result)
        self.assertEqual(d.hook_text(result, '/bin/bash /usr/lib/decky-nvidia-update/repair.sh "$UPDATED_SLOT"'), result)

    def test_unsupported_or_duplicate_activation_rejected(self):
        for text in (VALVE.replace('set-mode reboot', 'set-mode new-mode'),
                     VALVE + d.ANCHORS[0], VALVE.replace('steamos-finalize-install', 'unknown'),
                     '#!/bin/sh\n' + VALVE, '\n'.join(reversed(VALVE.splitlines()))):
            with self.subTest(text=text), self.assertRaises(ValueError):
                d.hook_text(text, '/bin/true')

    def test_native_policy_preserved_on_destination(self):
        native = VALVE.replace(d.ANCHORS[0], d.NATIVE_START + '/native/repair.sh other\n' + d.NATIVE_END + d.ANCHORS[0])
        self.assertEqual(d.hook_text(native, '/standalone'), native)
        manual = d.hook_text(native, '/standalone', temporary=True)
        self.assertNotIn('/native/repair.sh', manual)
        self.assertIn('/standalone', manual)
        self.assertIn('/native/repair.sh', native)

    def test_truncated_hook_rejected(self):
        with self.assertRaises(ValueError):
            d.hook_text(VALVE + d.HOOK_START, '/bin/true')


class Pins(unittest.TestCase):
    def test_exact_complete_set(self):
        values = d.package_pins(dict(version=VERSION, urls=URLS))
        self.assertEqual([v[0] for v in values], ['nvidia-utils', 'nvidia-open-dkms', 'lib32-nvidia-utils'])

    def test_reject_missing_duplicate_skew_and_local_urls(self):
        for urls in (' '.join(URLS.split()[:2]), URLS + ' ' + URLS.split()[0],
                     URLS.replace('lib32-nvidia-utils-610.57.04', 'lib32-nvidia-utils-610.99.00'),
                     URLS.replace('https://archive.archlinux.org', 'file:///tmp'),
                     URLS + '\n$(touch /tmp/oops)'):
            with self.subTest(urls=urls), self.assertRaises(ValueError):
                d.package_pins(dict(version=VERSION, urls=urls))

    def test_different_companion_pkgrel_allowed(self):
        urls = URLS.replace('lib32-nvidia-utils-610.57.04-1', 'lib32-nvidia-utils-610.57.04-2')
        self.assertEqual(d.package_pins(dict(version=VERSION, urls=urls))[2][1], '610.57.04-2')

    def test_record_preserves_native_config_and_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native = root / 'usr/lib/steamos-nvidia/driver.conf'
            native.parent.mkdir(parents=True)
            native.write_text("DRIVER_SPEC=old\nDRIVER_VERSION=old\nPKG_URLS=old\nTRIM_CUDA=1\nADD_XPADNEO=1\nXPADNEO_VERSION=v0.10.4\nXPADNEO_SHA256='abc'\n")
            post = root / 'usr/lib/rauc/post-install.sh'
            post.parent.mkdir(parents=True)
            post.write_text(VALVE)
            with patch.object(d, 'write_json', side_effect=lambda p, v: p.write_text(json.dumps(v))):
                d.record(tmp, VERSION, '6.16-neptune', URLS)
            text = native.read_text()
            self.assertIn('TRIM_CUDA=1', text)
            self.assertIn('ADD_XPADNEO=1', text)
            self.assertIn("XPADNEO_SHA256='abc'", text)
            self.assertIn('DRIVER_VERSION=' + VERSION, text)
            self.assertIn(d.HOOK_START, post.read_text())
            self.assertTrue((root / 'usr/lib/decky-nvidia-update/driver.py').is_file())


class Pruning(unittest.TestCase):
    def setup_tree(self, root):
        lib = root / 'usr/lib'
        db = lib / 'holo/pacmandb/local'
        for package, files in [('nvidia-utils-1-1', ['usr/lib/old.so', 'usr/lib/shared.so', 'usr/lib/keep.so']),
                               ('other-1-1', ['usr/lib/shared.so'])]:
            directory = db / package
            directory.mkdir(parents=True)
            (directory / 'files').write_text('%FILES%\n' + '\n'.join(files) + '\n')
        for name in ('old.so', 'shared.so', 'keep.so', 'unrelated.so'):
            (lib / name).write_text('payload')
        wanted = root / 'wanted'
        wanted.write_text('usr/lib/keep.so\n')
        return lib, db, wanted

    def test_reclaims_only_obsolete_unshared_driver_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib, db, wanted = self.setup_tree(root)
            d.prune(root, wanted)
            self.assertFalse((lib / 'old.so').exists())
            for name in ('shared.so', 'keep.so', 'unrelated.so'):
                self.assertTrue((lib / name).exists())

    def test_bad_manifest_fails_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib, db, wanted = self.setup_tree(root)
            with (db / 'nvidia-utils-1-1/files').open('a') as out:
                out.write('../../outside\n')
            with self.assertRaises(ValueError):
                d.prune(root, wanted)
            self.assertTrue((lib / 'old.so').exists())


class Transactions(unittest.TestCase):
    def check_install_failure(self, wrote, guard_failure=False):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            previous = dict(status='ready', source='B', target='A')
            info = dict(enough_space=True, source='A', target='B', devices={'A/rootfs': '/dev/source'})
            calls = []
            def fail(command, **kwargs):
                calls.append(command)
                if str(d.HERE / 'shutdown.py') in command:
                    if guard_failure:
                        raise RuntimeError('injected shutdown guard failure')
                    return
                if wrote:
                    (Path(command[-1]) / 'writing').touch()
                raise RuntimeError('injected staging failure')
            with patch.object(d, 'STATE', state), patch.object(d, 'private_dir'), patch.object(d, 'read_state', return_value=previous), patch.object(d, 'preflight', return_value=info), patch.object(d, 'run', return_value='source-uuid'), patch.object(d, 'root_identity', return_value='identity'), patch.object(d, 'write_json', side_effect=lambda p, v: p.write_text(json.dumps(v))), patch.object(d.subprocess, 'run', side_effect=fail), patch.object(d, 'protect') as protect:
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    d.install(VERSION)
                saved = json.loads((state / 'transaction.json').read_text())
                self.assertIn(str(d.HERE / 'shutdown.py'), calls[0])
                self.assertEqual(len(calls), 1 if guard_failure else 2)
                if wrote:
                    protect.assert_called_once_with('B')
                    self.assertEqual(saved['status'], 'failed')
                else:
                    protect.assert_not_called()
                    self.assertEqual(saved, previous)

    def test_failed_build_preserves_previous_rollback(self):
        self.check_install_failure(False)

    def test_failed_write_disables_target_and_records_failure(self):
        self.check_install_failure(True)

    def test_failed_shutdown_guard_stops_before_staging_and_preserves_rollback(self):
        self.check_install_failure(False, guard_failure=True)

    def test_bad_driver_request_has_no_system_calls(self):
        with patch.object(d, 'preflight') as preflight:
            for value in ('../610', '610.1;reboot', '610.1\n', ''):
                with self.assertRaises(ValueError):
                    d.install(value)
            preflight.assert_not_called()

    def test_low_space_stops_before_creating_transaction(self):
        with patch.object(d, 'preflight', return_value={'enough_space': False, 'required_bytes': 17 * 1024**3}), patch.object(d, 'private_dir') as mkdir:
            with self.assertRaisesRegex(ValueError, 'Need'):
                d.install(VERSION)
            mkdir.assert_not_called()

    def test_rollback_refuses_replaced_layout(self):
        with patch.object(d, 'read_state', return_value=dict(status='ready', source='A', target='B', devices={'old': 'disk'})), patch.object(d, 'layout', return_value=('A', 'B', {'new': 'disk'})), patch.object(d.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'layout'):
                d.rollback()
            run.assert_not_called()

    def test_rollback_rejects_failed_transaction(self):
        with patch.object(d, 'read_state', return_value=dict(status='failed')), patch.object(d, 'layout') as layout:
            with self.assertRaises(ValueError):
                d.rollback()
            layout.assert_not_called()

    def test_identity_changes_with_modules_and_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / d.MANIFEST
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"buildid":"test"}')
            module = root / 'usr/lib/modules/test/updates/dkms/nvidia.ko.zst'
            module.parent.mkdir(parents=True)
            module.write_bytes(b'old')
            before = d.root_identity(root, 'uuid-a')
            self.assertNotEqual(before, d.root_identity(root, 'uuid-b'))
            module.write_bytes(b'new')
            self.assertNotEqual(before, d.root_identity(root, 'uuid-a'))


if __name__ == '__main__':
    unittest.main()
