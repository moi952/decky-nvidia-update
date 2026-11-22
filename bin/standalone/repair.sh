#!/bin/bash
# Runs before Valve activates an inactive slot, after /etc and EFI migration.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/common.sh"
SLOT=${1:?slot required}
MODE=${2:-repair}
FINISH_WORK=${3:-}
[[ "$MODE" == repair || "$MODE" == --finish ]] || die "Unknown repair mode"
if [[ ${DECKY_NVIDIA_MOUNT_NS:-0} != 1 ]]; then
    exec unshare --mount --propagation slave env DECKY_NVIDIA_MOUNT_NS=1 /bin/bash "$0" "$@"
fi
mount -o remount,bind,noatime /var
mount -o remount,bind,noatime /etc
exec 9>/run/decky-nvidia-repair.lock
flock -n 9 || die "Another NVIDIA repair is running"
phase=$(python3 -I "$HERE/phase.py" target "$SLOT")
eval "$phase"
[[ -b ${ROOT_DEV:-} && -b ${EFI_DEV:-} ]] || die "Invalid repair devices"
WORK=$(mktemp -d /home/.decky-nvidia-repair.XXXXXX)
chmod 700 "$WORK"
ROOT="$WORK/root"
mkdir "$ROOT"
mounted=0
cleanup() {
    local rc=$?
    set +e
    if [[ $mounted == 1 ]]; then
        sync -f "$ROOT"
        btrfs property set "$ROOT" ro true
        umount -R "$ROOT" || return "$rc"
    fi
    rm -rf -- "$WORK"
    return "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mount -o compress-force=zstd:3 "$ROOT_DEV" "$ROOT"
mounted=1
btrfs property set "$ROOT" ro false
if [[ "$MODE" == repair ]]; then
    phase=$(python3 -I "$HERE/phase.py" pins)
    eval "$phase"
    kernels=()
    for p in "$ROOT/usr/lib/modules/"*neptune*; do [[ ! -d "$p" ]] || kernels+=("$(basename "$p")"); done
    [[ ${#kernels[@]} == 1 ]] || die "Cannot select one target kernel"
    build_payload "$HERE/payload.sh" "$WORK/build" --target "$ROOT" --kernel "${kernels[0]}" --driver "$DRIVER" --pins "$HERE/installed.json" --force -y
fi
phase=$(python3 -I "$HERE/phase.py" installed "$ROOT")
eval "$phase"
[[ -n ${VERSION:-} && -n ${KERNEL:-} ]] || die "Missing target driver receipt"
mkdir -p "$ROOT/efi"
mount "$EFI_DEV" "$ROOT/efi"
mount -t proc proc "$ROOT/proc"
mount --rbind /dev "$ROOT/dev"; mount --make-rslave "$ROOT/dev"
mount --rbind /sys "$ROOT/sys"; mount --make-rslave "$ROOT/sys"
finish_target "$ROOT" "$KERNEL"
if [[ "$MODE" == --finish ]]; then
    echo '[phase] verify Verifying the prepared system'
fi
check_driver "$ROOT" "$KERNEL" "${VERSION%-*}"
if [[ "$MODE" == --finish ]]; then
    [[ -f "$FINISH_WORK/request.json" ]] || die "Missing driver transaction"
    python3 -I "$HERE/phase.py" ready "$FINISH_WORK" "$ROOT" "$ROOT_DEV"
fi
echo "NVIDIA $VERSION validated in inactive slot $SLOT"
