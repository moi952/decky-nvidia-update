#!/bin/bash
# Fresh compressed clone, then Valve settings/boot migration into the other slot.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/common.sh"
WORK=${1:?private request directory required}
phase=$(python3 -I "$HERE/phase.py" request "$WORK")
eval "$phase"
[[ -n ${SOURCE_DEV:-} && -n ${TARGET_DEV:-} ]] || die "Invalid staging request"
[[ -f /etc/passwd && -e /etc/os-release ]] || die "Live /etc is incomplete; refusing configuration migration"
[[ $(systemctl show rauc.service -p UnitFileState --value) != masked* ]] || die "RAUC is already masked"
masked=0; loop=; watchdog=; migrating=0
cleanup() {
    local rc=$?
    set +e
    [[ -z $watchdog ]] || kill "$watchdog" 2>/dev/null
    [[ $migrating == 0 ]] || fsfreeze -u /var 2>/dev/null
    if [[ $rc != 0 && -e "$WORK/writing" ]]; then
        steamos-bootconf --image "$TARGET" config --no-create --set image-invalid 1 --set boot-requested-at 0 --set boot-attempts 0
        sync -f /esp/SteamOS/conf
    fi
    if mountpoint -q "$WORK/root"; then umount -R "$WORK/root"; fi
    if mountpoint -q "$WORK/source"; then umount "$WORK/source"; fi
    [[ -z $loop ]] || losetup -d "$loop"
    [[ $masked == 0 ]] || systemctl unmask --runtime rauc.service
    return "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
systemctl mask --runtime rauc.service
masked=1
systemctl stop rauc.service
# Keep atime writes out of the var-backed etc overlay during Valve migration.
mount -o remount,bind,noatime /var
mount -o remount,bind,noatime /etc
mkdir "$WORK/source" "$WORK/root"
truncate -s "$TARGET_BYTES" "$WORK/root.img"
mkfs.btrfs -q -f -L "rootfs-$TARGET" "$WORK/root.img"
loop=$(losetup --find --show "$WORK/root.img")
mount -o ro "$SOURCE_DEV" "$WORK/source"
python3 -I "$HERE/etc_config.py" check-legacy "$WORK/source/etc"
mount -o compress-force=zstd:3 "$loop" "$WORK/root"
echo '[phase] clone Preparing the system image on /home'
rsync -aHAXx --numeric-ids "$WORK/source/" "$WORK/root/"
python3 -I "$HERE/etc_config.py" migrate-legacy /etc "$WORK/source/etc" "$WORK/root/etc" /var/lib/overlays/etc/upper
# Keep user additions/edits in Valve's upper layer. Flattening the merged /etc
# here makes --compare-dest drop them, so a later OS update loses them.
# Reflect only deletions into this clone, including paths outside the keep list.
python3 -I "$HERE/etc_config.py" prune-deleted /etc "$WORK/root/etc"
require_space "$WORK/root" 256
[[ $(blkid -s UUID -o value "$loop") != $(blkid -s UUID -o value "$SOURCE_DEV") ]] || die "Duplicate filesystem UUID"
KERNEL=$(uname -r)
# Arch installs have no pin file; the worker rejects an empty --pins value.
echo '[phase] payload Preparing the driver in the system image'
build_payload "$HERE/payload.sh" "$WORK/build" --target "$WORK/root" --build-etc /etc --kernel "$KERNEL" --driver "$DRIVER" ${PINS:+--pins "$PINS"} --force -y
btrfs filesystem sync "$WORK/root"
btrfs property set "$WORK/root" ro true
umount "$WORK/root"
umount "$WORK/source"
image_uuid=$(blkid -s UUID -o value "$loop")
losetup -d "$loop"; loop=
python3 -I "$HERE/phase.py" image-released "$WORK/root.img" "$image_uuid"
python3 -I "$HERE/phase.py" recheck "$WORK"
echo '[phase] write Writing the prepared image to the inactive system'
# Protect before the first raw write. A failed migration must never select it.
touch "$WORK/writing"
steamos-bootconf --image "$TARGET" config --no-create --set image-invalid 1 --set boot-requested-at 0 --set boot-attempts 0
sync -f /esp/SteamOS/conf
dd if="$WORK/root.img" of="$TARGET_DEV" bs=16M conv=fsync status=progress
echo '[phase] finalize Migrating system settings and boot files'
rm -- "$WORK/root.img"
export RAUC_UPDATE_SOURCE="$WORK" RAUC_BUNDLE_MOUNT_POINT="$WORK"
migrating=1
(sleep 120; fsfreeze -u /var 2>/dev/null || true) &
watchdog=$!
# A temporary copy replaces existing NVIDIA hooks only for this transaction.
# The active root and its installed update policy remain untouched.
mount --bind "$WORK/post-install.sh" /usr/lib/rauc/post-install.sh
/bin/bash /usr/lib/rauc/post-install.sh
kill "$watchdog" 2>/dev/null || true
watchdog=; migrating=0
[[ -s "$WORK/ready.json" ]] || die "Valve migration did not finish NVIDIA verification"
echo 'Inactive system prepared; waiting for the transaction receipt'
