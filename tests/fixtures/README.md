# SteamOS migration fixtures

`holo-sync-var` and `atomic-update-keep.conf` are unmodified text fixtures from
SteamOS 3.9.5, build 20261023.100. The former retains Valve/Collabora's copyright
and LGPL-2.1+ notice. They are test data, not part of the plugin ZIP.

`linux_etc_persistence.py` executes only the captured `rsync_etc_overlay` function
on synthetic temporary files. It does not execute the full OS update script,
freeze a system partition, or access a real device. Run it in a disposable Linux
container with `CAP_SYS_ADMIN`, overlayfs/tmpfs support, and no host filesystem or
device mounts. From the repository root inside that container:

```sh
python3 tests/linux_etc_persistence.py
```

The test covers a fresh configuration, the old flattening regression, later
OS migration and shutdown catchup, and enabled/disabled/masked legacy services.
Legacy copy-up also runs with `metacopy=on`. The production payload setup block
is tested with both live and destination-OS package-manager settings.

This checks filesystem behavior, not a hardware boot or complete driver build.
