# SteamOS A/B driver preparation

A Decky Loader interface for preparing an NVIDIA driver in SteamOS's inactive
A/B system slot. The ZIP includes its controller, package builder, slot staging,
rollback and OS-update repair code. **No separately installed
`steamos-nvidia-driver` or NVIDIA installer is required.**

This edition adapts the A/B method used by the desktop driver manager in
steamos-nvidia-installer. It replaces the original plugin's live-root copy.
The UI and driver version discovery originate in moi952/decky-nvidia-update.

## Changes from the original plugin

- Replace live-root driver installation with preparation of the inactive SteamOS
  A/B slot. Compressed staging avoids requiring an enlarged 8 GiB root partition.
- Bundle the controller, isolated package builder, rollback and OS repair runtime.
  Work continues in a systemd service when the Decky panel closes.
- Preserve the recognized native steamos-nvidia update hook and driver settings.
- Offer original Arch packages and additional NVIDIA releases, with source/channel
  labels, counts and a scrollable version picker that fits the SteamOS panel.

## Storage and supported systems

### Configuration persistence

Driver preparation previously copied the merged live `/etc` into the immutable
root image. Valve's migration then omitted matching
upper-layer files; a subsequent SteamOS update could lose Decky's service, SSH
autostart and other user configuration.

Driver preparation now clones the base without importing live additions or edits.
Pacman configuration, mirrors and keyring are available only in the disposable
build overlay. OS-update repairs use the destination OS's repository settings.
Deleted base entries are pruned from the disposable clone without following
symlinks or entering live nested mounts. A read failure, unexpected target mount,
or more than 200 deleted entries stops preparation before writing a slot.

Valve's keep list remains authoritative for subsequent OS updates; this does not
add a blanket rule to preserve every `/etc` file. In particular, Valve's migration
does not copy opaque-directory overlay attributes. This build does not promise
to preserve arbitrary settings outside Valve's supported migration policy.

**Existing flattened bases:** when these four paths are present in the source
base, driver preparation moves their current effective state into the writable
overlay: `systemd/system/plugin_loader.service`, its
`multi-user.target.wants/plugin_loader.service` link, and
`multi-user.target.wants/sshd.service`, and a local `systemd/system/sshd.service`
override or mask. It verifies the copied contents and metadata
before removing those entries from the disposable base. It never enables a
disabled service, removes a mask, starts Decky/SSH, or writes the active base.
Copy-up changes storage location and inode metadata, but not effective service
configuration. Unsupported mounts, types or incomplete copy-up stop preparation.

This is a limited migration, not a complete detector or repair of every previously
baked-in setting. Existing base passwords and SSH host keys need separate review
before an OS update. It cannot recover configuration already lost by an earlier
OS update. Fresh upper-layer account settings and keys use normal Valve migration.

Installing the ZIP only replaces the plugin. It does not restore a missing Decky
service, enable SSH, change the NVIDIA driver, or select a boot slot. Driver
changes and reboots have been validated on the configuration described in
[the Stable validation record](validation-stable-20261024.md). A subsequent
SteamOS update with these changes still needs hardware validation.

### Layout

- An installed SteamOS A/B layout with Btrfs root filesystems and separate /home.
  Both slot sets must be on the same disk; ambiguous or mounted targets are refused.
- Modern `steamos-bootconf`, `steamos-chroot`, RAUC and the recognized Valve
  post-install finalization sequence. Unknown layouts/scripts are refused.
- NVIDIA GeForce GTX 16 or RTX GPUs with a documented match in the selected
  NVIDIA release. Legacy proprietary-module GPUs are outside this edition's scope.
- Decky Loader with root backend support, Python 3.10+ and SteamOS's normal build,
  filesystem and update utilities. Missing prerequisites are reported.
- Free space on /home: **the inactive root partition's size plus 12 GiB**
  (about 17 GiB for a 5 GiB root slot). Temporary images and build files live here.
- **No fixed 8 GiB root requirement and no partition or filesystem resize.**
  The root image has the existing inactive partition's size. Btrfs Zstd compression,
  removal of obsolete driver files and actual post-write space checks determine
  whether the chosen system/driver fits. At least 256 MiB must remain on root.
  This is not a promise that every future driver fits every 5 GiB installation.

The version list also includes NVIDIA releases from 590 onward that Arch has not
packaged. They are marked **Needs build**. Selecting one builds the three required
packages from Arch packaging recipes using NVIDIA's downloads, then prepares the
inactive slot in the same systemd job. Closing Decky does not interrupt this chain.
Old releases may be incompatible with current recipes, the GPU or SteamOS glibc;
these checks fail before writing the inactive slot. A catalog entry is not a
recommendation; NVIDIA's historical "Recommended" label is omitted from the list.

**Recommended (Arch)** filters all original Arch packages. NVIDIA-source entries
use NFB/Beta when NVIDIA provides that classification, otherwise **Other NVIDIA**.
Every option names its source (`Arch`, `Arch testing`, or `NVIDIA`); NVIDIA builds
also show `NFB`, `Beta` or `Other` and `Needs build`/`Compiled`. The filters show
counts of distinct versions. Versions already packaged by Arch appear only in
Recommended (Arch), so an NVIDIA channel can legitimately have zero entries.
Build status is a label, not a second filter hiding the chosen channel.
The update-channel
setting uses the same classification, so historical NVIDIA labels cannot select
an older driver as the recommended Arch update.

Custom packages are retained under `/home/.decky-nvidia-update/custom`, owned by
root, with package metadata and SHA-256 checks. Completed builds are never
overwritten. Packages needed by either OS slot are retained for a later SteamOS
update or rollback; confirmed-unused builds are collected automatically as
described below. Keep the retained directory when removing Decky.
They use /home space, not additional root space. Missing or modified retained
packages stop preparation instead of silently substituting an Arch version.

### Cache cleanup and migration

After a successful driver preparation or rollback selection, the controller
attempts automatic cleanup. It reads both `installed.json` and native
`steamos-nvidia/driver.conf` pins, wherever present, from **both A/B slots**.
Packages referenced by either slot remain, including while a reboot is pending
or a prepared slot has been disabled by cancellation. A standalone build is kept
when built; if it remains unreferenced, a later successful preparation may collect it.

Inspection mounts the inactive Btrfs root read-only in a private namespace with
log replay disabled. A dirty/unknown Btrfs log, unreadable slot, malformed pins,
NVIDIA installation without saved pins, unfinished transaction or leftover driver
workspace defers deletion. Collection shares the manual updater lock and the
standalone/native repair locks, and checks that SteamOS/RAUC are idle. Unknown
cache files, unsafe ownership, links and checksum failures are preserved with
`[cache]` diagnostics. Cleanup is best effort: failure does not change a successful
driver transaction into a failed one.

On plugin startup and before a custom build, the controller also attempts to
migrate compatible builds from `/home/.steamos-nvidia-custom-builds`. The original
flat layout has no SHA-256 manifest, so migration validates its package list and
each package's `.PKGINFO`, copies the three required packages into the new layout,
and verifies a new SHA-256 manifest before publishing the complete build. This
temporarily needs space for another copy. Existing retained builds are never
replaced, and migration does not rewrite either slot's saved URLs.

Startup migration alone removes nothing. A later cleanup removes validated legacy
contents only when neither slot refers to them. Referenced legacy paths and their
migrated copies remain available. Conflicting, incomplete, unsupported or otherwise
uncertain builds remain in place with a diagnostic. Validated unused split-package
outputs, such as OpenCL packages, are removed along with their unused legacy build.
The original legacy scripts do not participate in the new locks; maintenance is
deferred if a legacy driver command is detected. Do not run retired standalone
scripts concurrently with the plugin.

## How preparation works

1. Validate the booted root, both A/B slot sets, updater state, supported Valve
   migration script and available /home space. Share the desktop updater's lock.
2. Copy the active root into a fresh compressed Btrfs image on /home with a new
   filesystem UUID, sized to the inactive root partition.
3. Resolve matching driver packages, check the GPU and glibc requirements, and
   compile against the exact target kernel in a disposable overlay on /home.
4. Replace obsolete NVIDIA files in the image, copy the new payload, preserve
   any native integration configuration, and verify all four modules and 32/64-bit
   graphics-library dependencies. Build tools stay in the disposable overlay.
5. Mark the inactive slot invalid, write the prepared image to that slot, and let
   Valve migrate settings and boot files. Reapply NVIDIA boot settings, regenerate
   initramfs, and validate the target before Valve may select it for the next boot.

**Preparation replaces the previous contents of the inactive root/var/EFI slot.**
The active root is not unlocked or overwritten. The current system is retained as
rollback. Games and user files are not installation targets. A/B boot metadata is
updated; this is not a file-only operation.

The process runs as a systemd service and keeps its log under `operations/` in Decky's plugin log directory, outside Decky's rotating backend logs. Open **View log** for the saved output. Live mode follows the newest line; press the left stick (L3) to browse with the right stick, and press L3 again to return to Live and normal navigation. The log also supports clicking to switch modes, plus mouse-wheel and keyboard scrolling in history mode.
Closing/reloading Decky does not own or terminate that service. Reboot is explicit.
The UI distinguishes the current system from the driver prepared for the next boot.

Rollback validates both saved filesystem UUIDs, OS manifests, driver package
metadata and module contents. It refuses a stale transaction after either system
was replaced. Cancellation before the first reboot disables the prepared slot.

## Subsequent SteamOS updates

On a plain supported SteamOS installation, a small bundled runtime is saved in
`/usr/lib/decky-nvidia-update` in the prepared system. Its RAUC hook restores the
exact saved package versions for the new target kernel before slot activation.
If the packages, exact headers or supported update script are unavailable, the
new slot is not activated. The runtime propagates to the new system.

On systems with the recognized steamos-nvidia shared update hook, that integration
continues to own OS repair. Its `driver.conf` pins are updated while preserving
`TRIM_CUDA`, xpadneo and other settings. The manual transaction temporarily replaces
its repair invocation inside a private mount namespace to avoid installing the old
pin again. The installed native hook is preserved.

Removing the Decky UI does not remove the installed driver or its OS repair runtime.

## Plugin updates

The original toolkit's plugin updater is enabled: startup notifications, manual
checks and the release picker use the repository in `project.config.json`.
Decky Loader still handles download, confirmation, installation and reload.
The Settings page waits for driver status and does not expose plugin installation
while a driver operation is running, because that operation uses the bundled files.

Local builds synchronize the frontend configuration from `project.config.json`.
The release workflow sets both copies to its publishing `$GITHUB_REPOSITORY` before
building, so releases from `moi952/decky-nvidia-update` continue updating from
upstream, while fork releases use the fork. Keep the plugin name and display name
unchanged and publish a newer stable version with the workflow's asset name
`decky-nvidia-update-v<VERSION>.zip`. Automatic checks use the latest stable release;
prereleases remain available in the manual release picker. Earlier standalone
development builds with self-updates disabled need a manual update first.

## Operation status and recovery

The panel restores a pending installation or rollback after Decky reloads. A
restart is offered only when the saved transaction's destination is still
selected for boot and differs from the running system. Returning to the already
running system cancels the prepared selection without asking for another reboot.

Progress describes the whole transaction. A custom build or payload worker
finishing its own steps cannot report 100% before the inactive slot is written
and validated. Completion requires a matching operation receipt after the service
stops. The percentage represents stages, not elapsed time or a time estimate.

Start failures, an already running operation and an unavailable status connection
have distinct messages. The panel reconnects to status without resubmitting an
installation. Closing Decky detaches its observer; the systemd service continues.
If status cannot be read, update actions remain unavailable until it is restored.

## CLI

```bash
sudo bash bin/steamos-nvidia-update.sh --preflight
sudo bash bin/steamos-nvidia-update.sh --driver 610.57.04-1
sudo bash bin/steamos-nvidia-update.sh --status
sudo bash bin/steamos-nvidia-update.sh --rollback
sudo bash bin/steamos-nvidia-build.sh --version 615.71.09
sudo python3 -I bin/standalone/driver.py install-custom 615.71.09
sudo python3 -I bin/standalone/driver.py cache-migrate
sudo python3 -I bin/standalone/driver.py cache-cleanup
```

`steamos-nvidia-update.sh --list` lists archived versions without root.
`steamos-nvidia-build.sh` only builds retained packages; `install-custom` builds
if needed and then prepares the inactive slot. The internal shell workers under
`bin/standalone` are invoked only by the controller.
`cache-migrate` only copies validated legacy builds. `cache-cleanup` additionally
performs the guarded two-slot collection described above; both log why maintenance
is deferred when the system cannot be checked safely.

## Build and checks

The update hook recognizes the older direct `steamos-finalize-install` sequence
and the reported SteamOS 3.10 `FINISH` fallback loop with `holo-bootconf` activation.
The complete loop and its error check must match before a repair hook is inserted;
unrecognized or ambiguous layouts still stop preflight before a slot is written.
Supporting that script layout does not by itself qualify every development-channel
build. Bash execution tests cover fallback finalizers and blocked activation after
finalization or repair failure.

Hardware validation on 2026-10-15 covered SteamOS Main 3.10 build
`20261013.1000`, kernel `7.2.7-valve2-1-neptune-72-g31504582a073`, and an RTX 5060:
the running Decky backend upgraded Arch NVIDIA 610.57.04 to 615.78.08 and rebooted
successfully on a 5 GiB read-only root. The previous Main 610 slot passed identity
verification. This qualifies that tested configuration, not all future Main builds.
On that Main build, Valve's `holo-post-update-shutdown.service` could attempt its
final configuration migration after `/efi` was unmounted. Before preparing a
driver, the plugin now arms a temporary systemd drop-in under `/run`. It runs
Valve's original helper in a private mount namespace, keeping EFI mounts available
and mounting missing EFI filesystems using SteamOS's existing partset aliases.
Missing EFI metadata or a failed mount stops the helper before migration.

The guard checks the known vendor unit and helper hashes, refuses custom service
overrides, and verifies the effective systemd configuration before staging.
It is only enabled for the recognized late-shutdown implementation from
`steamos-customizations 20260916.1-2`. Older Stable layouts without the shutdown
trigger need no override; unknown late-shutdown implementations stop preflight.
This conservative check may need updating for another Main build.

The drop-in expires at reboot. It writes neither the running root nor `/etc`,
requires no desktop helper, and cannot carry an old service override into a new
OS boot. Each driver update through the plugin checks and arms it again.
It is not a permanent fix for unrelated updates started outside the plugin.
If a later preparation step fails, the harmless guard can remain until reboot;
Valve's existing trigger condition still controls whether the service runs.

A repeat Main 610.57.04 to 615.78.08 update and reboot passed with the earlier
manual workaround removed. Shutdown migration completed without the EFI error,
the temporary override disappeared after boot, and both slot identities passed.
See [the validation record](validation-main-20261015.md) for the exact test scope.

Arch installs resolve packages from the selected version and do not pass a pin-file
argument. Custom installs and OS-update repair pass their saved package manifests.
The argv regression tests exercise the shell call sites and the real worker parser
without starting a build or touching either OS slot.

```bash
pnpm install --frozen-lockfile
python3 -m pip install --target py_modules --no-deps "git+https://github.com/moi952/decky-plugin-toolkit.git@v0.1.0"
python3 -m unittest discover -s tests -v
for script in bin/steamos-nvidia-update.sh bin/standalone/*.sh; do bash -n "$script"; done
pnpm exec tsc --noEmit
node tests/driver_dropdown.cjs
node tests/dropdown_geometry.cjs
node tests/plugin_updates.cjs
node tests/driver_operation.cjs
bash package.sh
```

The backend's only vendored dependency is decky-plugin-toolkit, pinned to v0.1.0.
`package.sh` bundles it and produces a ZIP in `packages/`.

Pacman configuration staging corrects a build-overlay copy failure
seen with compressed Btrfs roots: rsync tried to remove `btrfs.compression` through
an overlay backed by ext4 and stopped with error 23. Pacman configuration copies
exclude that filesystem-specific attribute while retaining permissions, ACLs and
other extended attributes. The root-only test below reproduces the original
failure on disposable Btrfs/ext4 images and exercises both driver preparation
and OS-repair configuration copies without writing to OS partitions:

```bash
sudo unshare --mount --propagation private python3 tests/linux_pacman_xattrs.py
```

The build wrapper also handles the obsolete
`blake2b_generic` request in Valve's SteamOS 3.9.5 initcpio hook. During package
hooks and final initramfs generation, a temporary install-hook copy selects
`blake2b` only when the requested kernel's module metadata confirms built-in
BLAKE2b support and the old module name is unavailable. Older kernels and other
errors retain normal behavior. The temporary override is removed after the
child process exits; Valve's hook, kdumpst configuration and active boot files
are not patched. A direct mkinitcpio test on the affected kernel succeeded with
its output confined to a disposable file. Two subsequent driver installations
and reboots also passed; see [the Stable validation record](validation-stable-20261024.md).

Build helpers run in a separate PID and mount namespace. Exiting the worker
terminates package-hook daemons and releases their overlay mounts before the
prepared Btrfs image is copied to the inactive slot. The write is refused if
the image still has a loop device or remains registered as mounted in Btrfs.
This fixes the `/etc` overlay `Stale file handle` failure caused by temporary
Btrfs filesystem IDs while the build image was still mounted.

The root-only regression test uses disposable image files under `/home`, with
no writes to OS partitions. On an affected SteamOS kernel it reproduces the old
failure, then verifies helper cleanup and successful `/etc` remounts after both
successful and failed workers:

```bash
sudo bash tests/linux_build_lifecycle.sh "$PWD/bin/standalone"
sudo env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_cache.py -v
sudo env PYTHONDONTWRITEBYTECODE=1 python3 tests/linux_cache_lifecycle.py
sudo python3 -B tests/linux_shutdown_namespace.py
```

The cache tests use temporary package/slot fixtures. The lifecycle test mounts a
disposable Btrfs image and verifies that inspection leaves its bytes unchanged,
preserves both slots' pins and cleans up mounts even when inspection fails. It also
checks that failed unmounts cannot trigger recursive deletion of a mounted root.
Neither cache test command collects the machine's real driver cache or writes to
its OS slots. The PR and release workflows run the cache fixtures with root
permissions as well as the ordinary controller tests.

The SteamOS-only shutdown probe hides EFI mounts in a private namespace to
reproduce the original lookup failure, tests recovery and a missing-device error,
and checks that the host mount remains unchanged. It never runs slot migration;
an actual plugin update and reboot are still needed to validate shutdown behavior.

This is a development implementation. Unit tests, syntax checks and frontend builds
are not SteamOS runtime acceptance. Before release, validate on an isolated 5 GiB
A/B SteamOS installation: upgrade, downgrade, insufficient space, interrupted write,
Decky reload, reboot, rollback, and a subsequent SteamOS update. Then check actual
NVIDIA graphics, Gaming/Desktop mode transitions and suspend on hardware.

## Credits and license

Original plugin by [moi952](https://github.com/moi952/decky-nvidia-update).
Standalone SteamOS adaptation by [60plus](https://github.com/60plus/decky-nvidia-update),
using the driver staging method from
[28allday/steamos-nvidia-installer](https://github.com/28allday/steamos-nvidia-installer).
The upstream installer
MIT notice is included in `bin/standalone/LICENSE.steamos-nvidia` and is propagated
with the installed runtime. The original plugin declares the MIT license.
