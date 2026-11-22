#!/bin/bash
# A/B staging and compressed payload checks adapted from steamos-nvidia-installer.
# See LICENSE.steamos-nvidia for the upstream MIT notice.
set -euo pipefail
die() { echo "[fail] $*" >&2; exit 1; }
build_payload() (
    # Package hooks start background GnuPG helpers. A plain chroot leaves them
    # alive, pinning the build overlay and its Btrfs lower image. Let the kernel
    # reap the entire build PID namespace before returning to the caller.
    local worker="$1" work="$2" rc=0 loops
    shift 2
    work=$(readlink -m -- "$work")
    case "$work" in
        /home/.decky-nvidia-update/prepare-*/build|/home/.decky-nvidia-repair.*/build) ;;
        *) die "Unexpected private build workspace" ;;
    esac
    [[ ! -e "$work" && ! -L "$work" && ! -e "$work.img" && ! -L "$work.img" ]] || die "Build workspace already exists"
    unshare --mount --propagation slave --pid --fork --kill-child --mount-proc \
        /usr/bin/env DECKY_NVIDIA_BUILD_NS=1 /bin/bash "$worker" --workdir "$work" "$@" || rc=$?
    # Only the child namespace mounted these paths. Do not remove any backing
    # file until its loop device is really gone (detach can be deferred).
    for ((attempt=0; attempt<100; attempt++)); do
        loops=$(losetup -j "$work.img") || die "Cannot check build loop devices"
        [[ -n $loops ]] || break
        sleep 0.1
    done
    [[ -z $loops ]] || die "Build image is still in use; refusing to continue"
    if mountpoint -q "$work" || mountpoint -q "$work/merged"; then
        die "Build mounts remain in the caller namespace"
    fi
    rm -rf -- "$work" "$work.img"
    return "$rc"
)
require_space() {
    sync -f "$1"
    local available
    available=$(df -Pm "$1" | awk 'END {print $4}')
    [[ "$available" =~ ^[0-9]+$ ]] && (( available >= $2 )) || die "Insufficient space on $1 (need $2 MiB)"
}
check_driver() {
    local root="$1" kernel="$2" version="$3" module path magic bits loader directory library
    for module in nvidia nvidia_modeset nvidia_drm nvidia_uvm; do
        path=$(modinfo -b "$root" -k "$kernel" -F filename "$module")
        [[ "$path" == "$root/"* && -s "$path" ]] || die "Missing $module in target"
        magic=$(modinfo -F vermagic "$path")
        [[ "${magic%% *}" == "$kernel" && $(modinfo -F version "$path") == "$version" ]] || die "Wrong $module version/kernel"
    done
    for bits in 64 32; do
        if [[ $bits == 64 ]]; then loader=/usr/lib/ld-linux-x86-64.so.2; directory=/usr/lib;
        else loader=/usr/lib/ld-linux.so.2; directory=/usr/lib32; fi
        for library in libGLX_nvidia.so.0 libEGL_nvidia.so.0; do
            chroot "$root" /usr/bin/env -i LC_ALL=C "$loader" --list "$directory/$library" || die "Unresolved NVIDIA $bits-bit dependencies"
        done
    done
    [[ -s "$root/usr/share/vulkan/icd.d/nvidia_icd.json" ]] || die "Missing Vulkan ICD"
}
finish_target() {
    local root="$1" kernel="$2"
    mkdir -p "$root/etc/modprobe.d"
    cat > "$root/etc/modprobe.d/99-nvidia-patch.conf" <<'EOF'
blacklist nouveau
options nouveau modeset=0
options nvidia-drm modeset=1 fbdev=1
options nvidia NVreg_PreserveVideoMemoryAllocations=1
EOF
    chroot "$root" systemctl enable nvidia-suspend nvidia-resume nvidia-hibernate
    local options='rd.driver.blacklist=nouveau modprobe.blacklist=nouveau nvidia-drm.modeset=1 nvidia-drm.fbdev=1'
    grep -q '^GRUB_CMDLINE_LINUX_DEFAULT="' "$root/etc/default/grub" || die "Unsupported GRUB configuration"
    grep -q 'rd.driver.blacklist=nouveau' "$root/etc/default/grub" ||
        sed -i -E "s#^(GRUB_CMDLINE_LINUX_DEFAULT=\")#\1$options #" "$root/etc/default/grub"
    chroot "$root" update-grub
    grep -q 'rd.driver.blacklist=nouveau' "$root/efi/EFI/steamos/grub.cfg" || die "Missing NVIDIA boot configuration"
    # A cloned initramfs could contain the previous NVIDIA modules.
    python3 -I "$HERE/initcpio_compat.py" "$root" mkinitcpio -P
    chroot "$root" depmod "$kernel"
    chroot "$root" ldconfig
    require_space "$root" 256
    sync -f "$root/efi"
}
