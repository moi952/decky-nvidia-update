"""Integration test: real overlay mounts + captured Valve rsync function.

Runs only in a disposable container with no PC disks or host filesystem mounts.
It does not run an OS update or a driver build.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
EVIDENCE = SOURCE / 'tests/fixtures'
spec = importlib.util.spec_from_file_location('etc_config', SOURCE / 'bin/standalone/etc_config.py')
etc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(etc)
valve = (EVIDENCE / 'holo-sync-var').read_text()
function = valve[valve.index('rsync_etc_overlay() {'):valve.index('\nsync_var_mountpoints()')]


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


def put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def link(root, name, dest):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(dest)


mounts = []


def overlay(lower, upper, name, metacopy='off'):
    upper.mkdir(exist_ok=True)
    work = upper.with_name(upper.name + '-work')
    work.mkdir(exist_ok=True)
    merged = upper.with_name(name)
    merged.mkdir()
    run('mount', '-t', 'overlay', 'overlay', '-o',
        f'index=off,metacopy={metacopy},lowerdir={lower},upperdir={upper},workdir={work}', merged)
    mounts.append(merged)
    return merged


def migrate(source, dest, base, config):
    dest.mkdir(exist_ok=True)
    run('bash', '-euc', 'log(){ :; };\n' + function + '\nrsync_etc_overlay "$1" "$2" "$3" "" "$4"',
        '_', source, dest, config, base)


def present(view, expected_shadow='user-fixture'):
    assert (view / 'systemd/system/plugin_loader.service').read_text() == '[Service]\nExecStart=/home/deck/homebrew/services/PluginLoader\n'
    assert os.readlink(view / 'systemd/system/multi-user.target.wants/plugin_loader.service') == '/etc/systemd/system/plugin_loader.service'
    assert os.readlink(view / 'systemd/system/multi-user.target.wants/sshd.service') == '/usr/lib/systemd/system/sshd.service'
    assert (view / 'shadow').read_text() == expected_shadow
    assert (view / 'ssh/ssh_host_ed25519_key').read_text() == 'fixture-not-a-real-key'
    assert not (view / 'systemd/system/removed.service').exists()
    assert os.readlink(view / 'systemd/system/masked.service') == '/dev/null'


with tempfile.TemporaryDirectory(prefix='etc-lifecycle-') as tmp:
    root = Path(tmp)
    run('mount', '-t', 'tmpfs', '-o', 'size=64m', 'tmpfs', root)
    try:
        base = root / 'base/etc'
        base.mkdir(parents=True)
        put(base, 'shadow', 'stock-fixture')
        put(base, 'systemd/system/removed.service', 'stock unit')
        put(base, 'NetworkManager/conf.d/deleted.conf', 'intentionally deleted')
        put(base, 'modprobe.d/99-nvidia-patch.conf', 'old driver setting')
        live_upper = root / 'live-upper'
        live = overlay(base, live_upper, 'live')
        put(live, 'systemd/system/plugin_loader.service', '[Service]\nExecStart=/home/deck/homebrew/services/PluginLoader\n')
        link(live, 'systemd/system/multi-user.target.wants/plugin_loader.service', '/etc/systemd/system/plugin_loader.service')
        link(live, 'systemd/system/multi-user.target.wants/sshd.service', '/usr/lib/systemd/system/sshd.service')
        link(live, 'systemd/system/masked.service', '/dev/null')
        put(live, 'shadow', 'user-fixture')
        put(live, 'ssh/ssh_host_ed25519_key', 'fixture-not-a-real-key')
        (live / 'systemd/system/removed.service').unlink()
        (live / 'NetworkManager/conf.d/deleted.conf').unlink()
        config = root / 'filter'
        config.write_text((EVIDENCE / 'atomic-update-keep.conf').read_text().replace('/etc/', '/'))

        # Run the production source-base check and clone pruning.
        etc.check_legacy(base)
        cloned = root / 'cloned/etc'
        shutil.copytree(base, cloned, symlinks=True)
        etc.prune_deleted(live, cloned)
        assert (cloned / 'shadow').read_text() == 'stock-fixture'
        assert not (cloned / 'systemd/system/plugin_loader.service').exists()
        assert not (cloned / 'NetworkManager/conf.d/deleted.conf').exists()
        # Later driver-owned writes must survive the configuration handling.
        put(cloned, 'modprobe.d/99-nvidia-patch.conf', 'new driver setting')
        driver_upper = root / 'driver-upper'
        migrate(live_upper, driver_upper, cloned, config)
        driver_view = overlay(cloned, driver_upper, 'driver-view')
        present(driver_view)
        assert (driver_view / 'modprobe.d/99-nvidia-patch.conf').read_text() == 'new driver setting'

        # A later OS update supplies a fresh base and migrates only the upper.
        newbase = root / 'new-os/etc'
        shutil.copytree(base, newbase, symlinks=True)
        put(newbase, 'shadow', 'new-stock-fixture')
        os_upper = root / 'os-upper'
        migrate(driver_upper, os_upper, newbase, config)
        os_view = overlay(newbase, os_upper, 'os-view')
        present(os_view)
        run('umount', os_view)
        mounts.remove(os_view)

        # Simulate user's changes after initial sync, then Valve shutdown catchup.
        (driver_view / 'systemd/system/multi-user.target.wants/plugin_loader.service').unlink()
        put(driver_view, 'shadow', 'changed-before-reboot')
        migrate(driver_upper, os_upper, newbase, config)
        os_view2 = overlay(newbase, os_upper, 'os-view2')
        assert not (os_view2 / 'systemd/system/multi-user.target.wants/plugin_loader.service').is_symlink()
        assert (os_view2 / 'shadow').read_text() == 'changed-before-reboot'
        assert (os_view2 / 'systemd/system/plugin_loader.service').is_file()

        # Prove the original flattening loses the fixture in the same chain.
        old_flat = root / 'old-flat/etc'
        shutil.copytree(live, old_flat, symlinks=True)
        old_upper = root / 'old-upper'
        migrate(live_upper, old_upper, old_flat, config)
        lost_upper = root / 'lost-upper'
        migrate(old_upper, lost_upper, newbase, config)
        assert not (lost_upper / 'systemd/system/plugin_loader.service').exists()
        assert not (lost_upper / 'shadow').exists()
        print('PASS: actual overlay driver migration -> fresh OS migration -> shutdown catchup; old flattening loses service and shadow')

        for state in ('enabled', 'disabled', 'masked'):
            legacy = root / f'legacy-{state}'
            legacy.mkdir()
            put(legacy, 'systemd/system/plugin_loader.service', '[Service]\nExecStart=/home/deck/homebrew/services/PluginLoader\n')
            link(legacy, 'systemd/system/multi-user.target.wants/plugin_loader.service', '/etc/systemd/system/plugin_loader.service')
            link(legacy, 'systemd/system/multi-user.target.wants/sshd.service', '/usr/lib/systemd/system/sshd.service')
            if state == 'masked':
                link(legacy, 'systemd/system/sshd.service', '/dev/null')
            lup = root / f'legacy-{state}-upper'
            lv = overlay(legacy, lup, f'legacy-{state}-live', metacopy='on')
            if state == 'disabled':
                (lv / 'systemd/system/multi-user.target.wants/plugin_loader.service').unlink()
                (lv / 'systemd/system/multi-user.target.wants/sshd.service').unlink()
            if state == 'masked':
                (lv / 'systemd/system/plugin_loader.service').unlink()
                link(lv, 'systemd/system/plugin_loader.service', '/dev/null')
            dest = root / f'legacy-{state}-clone'
            shutil.copytree(legacy, dest, symlinks=True)
            original_base = {name: etc.signature(legacy / name) for name in etc.check_legacy(legacy)}
            etc.migrate_legacy(lv, legacy, dest, lup)
            assert not etc.check_legacy(dest)
            assert original_base == {name: etc.signature(legacy / name) for name in etc.check_legacy(legacy)}
            migrated = root / f'legacy-{state}-migrated'
            migrate(lup, migrated, dest, config)
            next_upper = root / f'legacy-{state}-next'
            migrate(migrated, next_upper, newbase, config)
            view = overlay(newbase, next_upper, f'legacy-{state}-final')
            unit = view / 'systemd/system/plugin_loader.service'
            if state == 'masked':
                assert os.readlink(unit) == '/dev/null'
                assert os.readlink(view / 'systemd/system/sshd.service') == '/dev/null'
            else:
                assert unit.read_text().startswith('[Service]')
            for name in ('plugin_loader.service', 'sshd.service'):
                path = view / 'systemd/system/multi-user.target.wants' / name
                assert path.is_symlink() == (state != 'disabled')
            print(f'PASS: legacy {state} service state preserved through real copy-up and two Valve migrations; source base unchanged')

        # Execute the actual payload setup block in real disposable overlays.
        payload = (SOURCE / 'bin/standalone/payload.sh').read_text()
        block = payload[payload.index('BUILD_ETC=${BUILD_ETC:-'):payload.index('mount -t proc proc "$MERGED/proc"')]
        lower = root / 'payload-base'
        host = root / 'host-etc'
        put(lower, 'etc/pacman.conf', '[jupiter-new-os]\n')
        put(lower, 'etc/pacman.d/mirrorlist', 'new-os-mirror\n')
        put(host, 'pacman.conf', '[jupiter-live]\n')
        put(host, 'pacman.d/mirrorlist', 'live-mirror\n')
        put(host, 'pacman.d/gnupg/private-keys-v1.d/test.key', 'fake-build-key')
        for mode in ('driver', 'os-repair'):
            merged = overlay(lower, root / f'build-{mode}-upper', f'build-{mode}-merged')
            run('bash', '-euc', 'die(){ echo "$*" >&2; exit 1; }; BUILD_ETC=$1; MERGED=$2; TARGET=$3;\n' + block,
                '_', host if mode == 'driver' else '', merged, lower)
            expected = '[jupiter-live]\n' if mode == 'driver' else '[jupiter-new-os]\n'
            assert (merged / 'etc/pacman.conf').read_text() == expected
            assert (lower / 'etc/pacman.conf').read_text() == '[jupiter-new-os]\n'
            assert not (lower / 'etc/pacman.d/gnupg').exists()
            assert (merged / 'etc/pacman.d/gnupg/private-keys-v1.d/test.key').exists() == (mode == 'driver')
            assert (host / 'pacman.d/gnupg/private-keys-v1.d/test.key').read_text() == 'fake-build-key'
            print(f'PASS: production payload setup ({mode}) uses correct repository; keyring remains build-only')
    finally:
        for path in reversed(mounts):
            run('umount', path)
        run('umount', root)
