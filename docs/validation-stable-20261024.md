# SteamOS Stable driver-change validation, 2026-10-24

## Tested configuration

- SteamOS Stable 3.9.5, build `20261023.100`.
- Kernel `7.2.7-valve2-2-neptune-72-g31504582a073`.
- NVIDIA GeForce RTX 5060, SteamOS A/B layout with 5 GiB root slots.
- Decky Loader 3.3.0 and the standalone updater backend in this branch.
- Existing SteamOS NVIDIA Installer 0.2.7 integration and Gamescope 3.17.0.

## Completed operations

1. Selected NVIDIA 615.71.09 while running 615.78.08. The plugin prepared slot A,
   validated its modules and libraries, generated initramfs and selected it for
   boot. After a user-initiated reboot, slot A loaded 615.71.09 successfully.
2. Selected NVIDIA 615.78.08 in the plugin. The plugin prepared a new installation
   in slot B. After another user-initiated reboot, slot B loaded 615.78.08.

The second operation was a driver installation, not the dedicated rollback
command. Both operations completed through the Decky interface.

Before each reboot, read-only checks verified the prepared slot's identity against
the transaction receipt, the target driver modules and kernel version, readable
initramfs, and the target EFI configuration. After each reboot, `nvidia-smi` and
the loaded kernel modules confirmed the requested version. The corresponding
32-bit and 64-bit NVIDIA userspace package versions matched. Neither boot showed
an NVIDIA Xid error in the kernel log examined during verification.

Decky's unit and autostart link remained in the writable `/etc` overlay. Decky
started automatically and all four installed plugins registered in Steam:
FileExplorer, Decky Localsend, Decky NVIDIA Update and Volume Boost.

SSH autostart was explicitly enabled by the owner before the first reboot. It
started automatically after both reboots, and its enabled state survived the
second driver installation. The updater itself did not enable SSH. Neither SSH
nor Decky recorded an automatic service restart during these checks.

The tester confirmed HDR, VRR, notifications and the in-game overlay worked on
both driver versions. The existing installer integration and custom Gamescope
were retained. These observations qualify this configuration, not every GPU,
SteamOS channel or integration.

## Failures reproduced and corrected

- Copying pacman configuration from compressed Btrfs through an ext4-backed
  overlay failed when rsync tried to remove `btrfs.compression`. The copy now
  excludes that filesystem-specific attribute while preserving other supported
  metadata. A disposable Btrfs/ext4 regression test covers both the driver-build
  and OS-repair copy paths.
- The SteamOS 3.9.5 initcpio hook requested the unavailable `blake2b_generic`
  module. A temporary hook copy selects `blake2b` only when the target kernel's
  metadata confirms built-in support. A disposable-output mkinitcpio probe and
  both complete driver installations succeeded. The installed Valve hook and
  kdumpst configuration are not changed by this compatibility wrapper.
- Synthetic overlay tests exercise Valve's configuration migration with the
  corrected base-only clone, including enabled, disabled and masked service
  states. They also reproduce the old flattening regression.

## Remaining coverage

A full SteamOS update after these fixes, the dedicated rollback command, and
failure cases such as an interrupted slot write were not part of this hardware
run. Suspend/resume and other GPU models were not tested here. Synthetic migration
tests do not replace that hardware coverage.
