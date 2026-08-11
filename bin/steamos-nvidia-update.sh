#!/bin/bash
#
# steamos-nvidia-update.sh — install or change the NVIDIA driver version on
# an ALREADY INSTALLED, running SteamOS system, in place.
#
# No USB stick, no repair image, no OS reinstall — this runs directly on the
# machine that needs the driver. Unlike steamos-nvidia-installer.sh (which
# builds a USB image from a clean OOBE repair image, necessarily pinned to
# whatever SteamOS version that repair image ships), this tool works against
# whatever SteamOS version is CURRENTLY running, however far it's drifted
# from Valve's repair image on the update branch you're on.
#
# The slow/risky part (download, dkms build, pacman transaction) happens in
# a throwaway overlay stacked on top of the running rootfs (read-only lower,
# read-write upper) — the real system is not touched until the very end,
# where a short rsync copies the finished driver files in. Games, saves,
# Steam login, Decky, everything under /home: untouched the whole time, on
# every code path, whether the run succeeds or is interrupted.
#
# SCOPE GUARANTEE (dual-boot safe): this script never calls losetup/mount on
# any block device other than a single ext4 loopback FILE it creates on
# /home. It never enumerates disks, never touches partition tables, and
# never mounts anything by device path. A Windows install on another
# partition or another disk is never opened, referenced, or written to
# anywhere in this file — there is simply no code path that looks at any
# device other than the one you're already booted from.
#
#   sudo ./steamos-nvidia-update.sh              CLI version picker
#   sudo ./steamos-nvidia-update.sh --gui        graphical (zenity) version picker
#   sudo ./steamos-nvidia-update.sh --list       show available versions, exit
#   sudo ./steamos-nvidia-update.sh --driver 580 non-interactive
#
# Options:
#   --driver SPEC      "latest" or a version prefix (580, 580.105.08, ...).
#                      Omit to pick from a menu (CLI, or --gui for a dialog).
#   --gui              Use a zenity dialog to pick the version and confirm,
#                       instead of the terminal menu. Progress/step messages
#                       still print to the terminal either way.
#   --list             Print recent available nvidia-utils versions and exit.
#   --force            Run the full pipeline (download, build, install) even
#                      if the chosen version matches what's already
#                      installed. Useful as a dry-run of the whole mechanism
#                      that ends with the exact same driver you started with.
#   -y, --yes          Don't ask for confirmation before installing.
#   --skip-sigcheck    Disable pacman signature checks in the build chroot.
#   --workdir DIR      Build dir (default: /home/.steamos-nvidia-update-work).
#
# Reruns from scratch every time (no cache) — this is meant to be run
# occasionally by hand, not repeatedly, so simplicity wins over resumability.
# A reboot is required afterwards to load the new module.

set -euo pipefail

log()  { printf '\e[1;35m[nvidia-update]\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m[warn]\e[0m %s\n' "$*" >&2; }
die()  { printf '\e[1;31m[fail]\e[0m %s\n' "$*" >&2; exit 1; }

TOTAL_STEPS=8
STEP=0
step() { STEP=$((STEP+1)); printf '\e[1;36m[%d/%d]\e[0m %s\n' "$STEP" "$TOTAL_STEPS" "$*"; }

# ------------------------------------------------------------------- args
DRIVER_SPEC=""
LIST_ONLY=0
GUI=0
FORCE=0
ASSUME_YES=0
SKIP_SIG=0
WORKDIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --driver)        DRIVER_SPEC="${2:?--driver needs an argument}"; shift ;;
    --list)          LIST_ONLY=1 ;;
    --gui)           GUI=1 ;;
    --force)         FORCE=1 ;;
    -y|--yes)        ASSUME_YES=1 ;;
    --skip-sigcheck) SKIP_SIG=1 ;;
    --workdir)       WORKDIR="${2:?--workdir needs an argument}"; shift ;;
    -h|--help)       sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)               die "Unknown option: $1" ;;
  esac
  shift
done

ARCHIVE_URL=https://archive.archlinux.org/packages

# ------------------------------------------------------------- version list
# No root/preflight needed just to browse versions — only curl matters here.
list_versions() {
  curl -sfL "$ARCHIVE_URL/n/nvidia-utils/" \
    | grep -oE 'nvidia-utils-[0-9][^"<]*-x86_64\.pkg\.tar\.zst' \
    | sed -E 's/^nvidia-utils-//; s/-x86_64\.pkg\.tar\.zst$//; s/-[0-9]+$//' \
    | sort -uV | tail -15
}

if [[ $LIST_ONLY -eq 1 ]]; then
  command -v curl >/dev/null || die "Missing tool: curl"
  log "Recent nvidia-utils versions on the Arch archive (newest last):"
  list_versions
  exit 0
fi

# --------------------------------------------------------- step 1: preflight
step "Preflight checks"

[[ $EUID -eq 0 ]] || die "Run as root (sudo)."
grep -q '^ID=steamos' /etc/os-release 2>/dev/null || warn "This doesn't look like SteamOS — continuing anyway."

for tool in curl pacman dkms chroot mount mkfs.ext4 tar zstd python3 rsync depmod readelf findmnt df; do
  command -v "$tool" >/dev/null || die "Missing tool: $tool"
done
if [[ $GUI -eq 1 ]]; then
  command -v zenity >/dev/null || die "--gui requires zenity, which isn't installed"
fi

# /home must be its own filesystem: this is where the build workspace lives
# (a loopback file, never a raw partition) and it's also the concrete check
# that confirms we're on a normal SteamOS layout, not something unexpected.
mountpoint -q /home || die "/home is not a separate mounted filesystem — refusing to continue (unexpected disk layout)"

AVAIL_MB="$(df -m --output=avail /home | tail -1 | tr -d ' ')"
(( AVAIL_MB > 10000 )) || die "Not enough free space on /home: need ~10 GB, have ${AVAIL_MB} MB"

if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -qi 'enabled'; then
  warn "Secure Boot appears to be enabled — an unsigned DKMS nvidia module may fail to load after reboot. Disable Secure Boot or enroll a MOK key if the driver doesn't come up."
fi

KVER="$(uname -r)"
CURRENT_VER="$(pacman -Q nvidia-utils 2>/dev/null | awk '{print $2}' || true)"
log "Running kernel: $KVER"
if [[ -n "$CURRENT_VER" ]]; then
  log "Currently installed nvidia-utils: $CURRENT_VER"
else
  log "No NVIDIA driver currently installed on this system."
fi

# --------------------------------------------------- step 2: choose version
step "Selecting driver version"

if [[ -z "$DRIVER_SPEC" ]]; then
  log "Fetching available versions from the Arch archive..."
  mapfile -t VERSIONS < <(list_versions)
  [[ ${#VERSIONS[@]} -gt 0 ]] || die "Could not fetch version list from $ARCHIVE_URL (no network?)"

  if [[ $GUI -eq 1 ]]; then
    ROWS=(FALSE latest "whatever current Arch ships")
    for v in "${VERSIONS[@]}"; do ROWS+=(FALSE "$v" ""); done
    DRIVER_SPEC="$(zenity --list --radiolist \
      --title "NVIDIA driver — choose a version" \
      --text "Currently installed: ${CURRENT_VER:-none}\n\nGames, saves, Steam login and Decky are never touched." \
      --column "" --column "Version" --column "" \
      --width 520 --height 420 "${ROWS[@]}")" || { log "Cancelled."; exit 0; }
    [[ -n "$DRIVER_SPEC" ]] || die "No version selected"
  else
    echo
    echo "  0) latest  (whatever current Arch ships)"
    for i in "${!VERSIONS[@]}"; do printf '  %d) %s\n' "$((i+1))" "${VERSIONS[$i]}"; done
    echo
    read -rp "Choose a driver [0]: " CHOICE
    CHOICE="${CHOICE:-0}"
    [[ "$CHOICE" =~ ^[0-9]+$ ]] || die "Invalid choice"
    if [[ "$CHOICE" -eq 0 ]]; then
      DRIVER_SPEC=latest
    else
      DRIVER_SPEC="${VERSIONS[$((CHOICE-1))]:-}"
      [[ -n "$DRIVER_SPEC" ]] || die "Invalid choice"
    fi
  fi
fi
[[ "$DRIVER_SPEC" == latest || "$DRIVER_SPEC" =~ ^[0-9]+(\.[0-9]+)*(-[0-9]+)?$ ]] \
  || die "--driver takes 'latest' or a version prefix like 580 / 580.105.08 / 580.105.08-4"
log "Selected: $DRIVER_SPEC"

CONFIRM_MSG="Install NVIDIA driver '$DRIVER_SPEC' on this system now?\n\nGames, saves, Steam login and Decky in /home are never touched. Other disks/partitions (including Windows, if dual-booting) are never touched either. A reboot is required afterwards."
if [[ $GUI -eq 1 ]]; then
  zenity --question --no-wrap --title "Confirm" --text "$CONFIRM_MSG" || { log "Aborted."; exit 0; }
elif [[ $ASSUME_YES -eq 0 ]]; then
  read -rp "$(printf '%b' "${CONFIRM_MSG//\\n/\n}") [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { log "Aborted."; exit 0; }
fi

[[ -n "$WORKDIR" ]] || WORKDIR="/home/.steamos-nvidia-update-work"
UPPER="$WORKDIR/upper"
OVLWORK="$WORKDIR/ovlwork"
MERGED="$WORKDIR/merged"
WORKIMG="$WORKDIR.img"   # ext4 loopback FILE on /home — never a raw partition.
                          # SteamOS /home is casefold ext4, which overlayfs
                          # rejects as an upperdir, hence a plain ext4 file.
ROOTFS_DISABLED=0

# ---------------------------------------------------------------- cleanup
cleanup() {
  set +e
  for m in "$MERGED"/dev/pts "$MERGED"/dev "$MERGED"/sys "$MERGED"/proc "$MERGED" "$WORKDIR"; do
    mountpoint -q "$m" 2>/dev/null && { umount -R "$m" 2>/dev/null || umount -Rl "$m" 2>/dev/null; }
  done
  rm -rf "$WORKDIR" "$WORKIMG"
  if [[ $ROOTFS_DISABLED -eq 1 ]]; then
    warn "Re-locking rootfs after an interrupted run"
    command -v steamos-readonly >/dev/null && steamos-readonly enable || btrfs property set / ro true
  fi
}
trap cleanup EXIT

rm -rf "$WORKDIR" "$WORKIMG"
mkdir -p "$(dirname "$WORKDIR")"
truncate -s 6G "$WORKIMG"
mkfs.ext4 -q -F "$WORKIMG"
mkdir -p "$WORKDIR"
mount -o loop "$WORKIMG" "$WORKDIR"
mkdir -p "$UPPER" "$OVLWORK" "$MERGED"

# ----------------------------------------- step 3: resolve driver packages
step "Resolving driver packages from Arch Linux ($DRIVER_SPEC)"

PKG_URLS=""
PKG_URL_ARR=()
PKG_FILES=()
PKG_NAMES=()
FETCHED=0
DRIVER_VERSION=""
NV_PKGVER=""
PIN_VER=""

pin_pkg() {
  local pkg="$1" spec="$2" repo ver file url attempt
  if [[ "$spec" == latest ]]; then
    # archlinux.org occasionally answers a plain 502 for a moment (seen in
    # practice) — fetch to a variable and retry a few times first, so a
    # single transient blip doesn't fail the whole run. Only pipe to python
    # once we actually have a non-empty response: previously an empty/error
    # response went straight into json.load(), which threw a raw Python
    # traceback into the log instead of a clean, actionable error.
    local json=""
    for attempt in 1 2 3; do
      json="$(curl -sfL "https://archlinux.org/packages/search/json/?name=$pkg" 2>/dev/null)"
      [[ -n "$json" ]] && break
      (( attempt < 3 )) && sleep 2
    done
    [[ -n "$json" ]] || die "archlinux.org isn't responding right now (already retried 3 times) — this is a server-side issue, not your system. Try again in a few minutes. ERR_ARCH_SERVER_UNREACHABLE"
    read -r ver file repo < <(printf '%s' "$json" | python3 -c 'import json,sys
r=[p for p in json.load(sys.stdin)["results"]
   if p["repo"] in ("core","extra","multilib") and p["arch"] == "x86_64"]
if not r: raise SystemExit(1)
p=r[0]; print(p["pkgver"]+"-"+str(p["pkgrel"]), p["filename"], p["repo"])' 2>/dev/null) \
      || die "$pkg is not currently in core/extra/multilib for x86_64 on archlinux.org"
    url="$ARCHIVE_URL/${pkg:0:1}/$pkg/$file"
    if ! curl -sfIL "$url" -o /dev/null; then
      url="https://geo.mirror.pkgbuild.com/$repo/os/x86_64/$file"
      curl -sfIL "$url" -o /dev/null || die "$pkg $ver not on archive.archlinux.org nor the mirror"
      warn "$pkg not yet in the Arch archive — pinning mirror URL (may go stale)"
    fi
  else
    # Same reasoning as the "latest" branch above: retry a few times before
    # concluding anything, and distinguish "the server didn't answer" from
    # "the server answered fine but this spec matches nothing" instead of
    # lumping both into one ambiguous "bad --driver value, or no network".
    local html=""
    for attempt in 1 2 3; do
      html="$(curl -sfL "$ARCHIVE_URL/${pkg:0:1}/$pkg/" 2>/dev/null)"
      [[ -n "$html" ]] && break
      (( attempt < 3 )) && sleep 2
    done
    [[ -n "$html" ]] || die "archive.archlinux.org isn't responding right now (already retried 3 times) — this is a server-side issue, not your system. Try again in a few minutes. ERR_ARCH_SERVER_UNREACHABLE"
    # The trailing "([.-][^"<]*)?" is OPTIONAL: a bare pkgver spec (e.g.
    # "610.43.03") needs it to reach across the "-N" pkgrel suffix to
    # "-x86_64...", but an exact pkgver-pkgrel spec (e.g. "610.43.03-5",
    # what the detailed/exact-build picker sends) is already immediately
    # followed by "-x86_64..." with nothing in between — making the group
    # mandatory (as it was) meant an exact spec could never match anything.
    file="$(printf '%s' "$html" \
            | grep -oE "${pkg}-${spec}([.-][^\"<]*)?-x86_64\.pkg\.tar\.zst" | sort -uV | tail -1 || true)"
    [[ -n "$file" ]] || die "No $pkg build matching '$spec' in the Arch archive — check the version number"
    ver="${file#"$pkg"-}"; ver="${ver%-x86_64.pkg.tar.zst}"
    url="$ARCHIVE_URL/${pkg:0:1}/$pkg/$file"
  fi
  PKG_URLS+="${PKG_URLS:+ }$url"
  PKG_URL_ARR+=("$url")
  PKG_FILES+=("$file")
  PKG_NAMES+=("$pkg")
  PIN_VER="$ver"
  log "  $pkg $ver"
}

# fetch_one <url> <dest.part> — downloads with periodic progress lines
# (packages here run ~280-300 MB; a silent multi-minute curl looks frozen).
fetch_one() {
  local url="$1" dest="$2" total have pct pid rc
  total="$(curl -sIL "$url" 2>/dev/null | tr -d '\r' \
    | awk 'BEGIN{IGNORECASE=1} /^content-length:/{v=$2} END{print v}')"
  curl -sfL "$url" -o "$dest" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    have="$(stat -c%s "$dest" 2>/dev/null || echo 0)"
    if [[ "$have" -gt 0 ]]; then
      if [[ -n "$total" && "$total" -gt 0 ]]; then
        pct=$(( have * 100 / total ))
        log "  ... ${pct}% ($(( have / 1048576 )) / $(( total / 1048576 )) MB)"
      else
        log "  ... $(( have / 1048576 )) MB"
      fi
    fi
    sleep 2
  done
  wait "$pid"; rc=$?
  return "$rc"
}

fetch_pins() {
  mkdir -p "$WORKDIR/pkgs"
  local i f
  for (( i=FETCHED; i<${#PKG_FILES[@]}; i++ )); do
    f="${PKG_FILES[$i]}"
    log "Downloading $f"
    fetch_one "${PKG_URL_ARR[$i]}" "$WORKDIR/pkgs/$f.part" || die "download failed: ${PKG_URL_ARR[$i]}"
    mv "$WORKDIR/pkgs/$f.part" "$WORKDIR/pkgs/$f"
  done
  FETCHED=${#PKG_FILES[@]}
}

pin_pkg nvidia-utils "$DRIVER_SPEC"
DRIVER_VERSION="$PIN_VER"; NV_PKGVER="${PIN_VER%-*}"

if [[ "$DRIVER_VERSION" == "$CURRENT_VER" && $FORCE -ne 1 ]]; then
  log "nvidia-utils $DRIVER_VERSION is already installed — nothing to do (use --force to run the full pipeline anyway)."
  exit 0
fi
[[ "$DRIVER_VERSION" == "$CURRENT_VER" ]] && log "Reinstalling the currently installed version ($DRIVER_VERSION) — --force, full pipeline will run."
log "Driver pinned: nvidia-open $DRIVER_VERSION"
fetch_pins

COMPANION_SPEC="$DRIVER_SPEC"
[[ "$COMPANION_SPEC" == latest ]] || COMPANION_SPEC="$NV_PKGVER"
for pkg in nvidia-open-dkms lib32-nvidia-utils; do
  pin_pkg "$pkg" "$COMPANION_SPEC"
  [[ "$PIN_VER" == "$NV_PKGVER"-* ]] \
    || die "Version skew: $pkg is $PIN_VER but nvidia-utils is $DRIVER_VERSION (mirror mid-update?) — retry in an hour"
done

ARCH_ONLY_DEPS=" egl-wayland2 "
while read -r dep; do
  [[ -n "$dep" && "$ARCH_ONLY_DEPS" == *" $dep "* ]] || continue
  log "  $DRIVER_VERSION also needs $dep, which Valve's repo predates"
  pin_pkg "$dep" latest
done < <(tar -xOf "$WORKDIR/pkgs/${PKG_FILES[0]}" .PKGINFO \
         | awk '$1 == "depend" { print $3 }' | sed 's/[<>=].*//')
fetch_pins

# ------------------------------------------ step 4: glibc compatibility
step "Checking glibc compatibility"

IMG_GLIBC="$(pacman -Q glibc | awk '{print $2}' | grep -oE '^[0-9]+\.[0-9]+')"
[[ -n "$IMG_GLIBC" ]] || die "Could not determine this system's glibc version"
log "This system's glibc: $IMG_GLIBC"
SCAN="$WORKDIR/glibc-scan"
mkdir -p "$SCAN"
for f in "${PKG_FILES[@]}"; do
  mkdir -p "$SCAN/${f%%.pkg.tar.zst}"
  tar -xf "$WORKDIR/pkgs/$f" -C "$SCAN/${f%%.pkg.tar.zst}"
done
MAX_GLIBC="$({ find "$SCAN" -type f \( -name '*.so*' -o -perm -111 \) \
  -exec readelf -V {} + 2>/dev/null || true; } | grep -o 'GLIBC_[0-9.]*' \
  | sed 's/^GLIBC_//' | sort -uV | tail -1)"
[[ -n "$MAX_GLIBC" ]] || die "glibc scan found no ELF version references — scan broken?"
if [[ "$(printf '%s\n' "$MAX_GLIBC" "$IMG_GLIBC" | sort -V | tail -1)" != "$IMG_GLIBC" ]]; then
  die "Driver payload needs glibc $MAX_GLIBC but this system only has $IMG_GLIBC — too far ahead of what this SteamOS release can run; wait for a newer SteamOS update or pick an older --driver"
fi
log "OK: payload needs at most glibc $MAX_GLIBC"
rm -rf "$SCAN"

# ------------------------------------------------- step 5: build in overlay
step "Building the driver (overlay chroot + dkms, this takes 10-20 min)"

# lowerdir is the LIVE running root — read-only access, never written here.
# All build residue (headers, dkms, gcc, the new driver files) lands in
# $UPPER; the real system is touched only by the copy in step 7.
mount -t overlay overlay -o "index=off,lowerdir=/,upperdir=$UPPER,workdir=$OVLWORK" "$MERGED"
mount -t proc proc "$MERGED/proc"
mount --rbind /sys "$MERGED/sys";  mount --make-rslave "$MERGED/sys"
mount --rbind /dev "$MERGED/dev";  mount --make-rslave "$MERGED/dev"
rm -f "$MERGED/etc/resolv.conf"
cp -L /etc/resolv.conf "$MERGED/etc/resolv.conf"

in_chroot() { chroot "$MERGED" /bin/bash -c "$*"; }

# --overwrite='*': the overlay's lowerdir is the LIVE system, which can
# already have stray files at paths dkms/headers/pahole want (e.g. from a
# previous interrupted run, or files pacman's local db doesn't know about)
# — pacman refuses to clobber untracked files by default. Safe here since
# we're inside a throwaway overlay; nothing real is touched until later.
PACOPTS="--noconfirm --needed --overwrite='*'"
PACCONF="/etc/pacman.conf"
if [[ $SKIP_SIG -eq 1 ]]; then
  sed 's/^SigLevel.*/SigLevel = Never/' "$MERGED/etc/pacman.conf" > "$MERGED/tmp/pacman-nosig.conf"
  PACCONF="/tmp/pacman-nosig.conf"
  warn "pacman signature verification DISABLED for the build"
fi

[[ -d "$MERGED/etc/pacman.d/gnupg/private-keys-v1.d" ]] \
  || in_chroot "pacman-key --init && pacman-key --populate" \
  || die "Keyring init failed — rerun with --skip-sigcheck if you accept unsigned installs"

# Exact-match kernel headers for the RUNNING kernel (same source Valve uses).
PACDB="/usr/lib/holo/pacmandb/local"
KPKG_DIR=""
for d in "$PACDB"/linux-neptune-*-[0-9]*; do
  [[ -d "$d" ]] || continue
  case "$(basename "$d")" in *-headers-*|*firmware*|*rtw*) continue ;; esac
  KPKG_DIR="$d"; break
done
[[ -n "$KPKG_DIR" ]] || die "Could not find installed kernel package in pacman db"
KPKG_FULL="$(basename "$KPKG_DIR")"
KPKG_NAME="${KPKG_FULL%-*-*}"
KPKG_VERREL="${KPKG_FULL#"$KPKG_NAME"-}"
JUPITER_REPO="$(awk -F'[][]' '/^\[jupiter-/{print $2; exit}' /etc/pacman.conf)"
MIRROR="$(awk '/^Server/{print $3; exit}' /etc/pacman.d/mirrorlist)"
HDR_URL="${MIRROR/\$repo/$JUPITER_REPO}"
HDR_URL="${HDR_URL/\$arch/x86_64}/${KPKG_NAME}-headers-${KPKG_VERREL}-x86_64.pkg.tar.zst"
curl -sfIL "$HDR_URL" -o /dev/null || die "Exact-match headers not found in Valve's pool: $HDR_URL"
log "Headers: $(basename "$HDR_URL")"

in_chroot "curl -sfL '$HDR_URL' -o /tmp/headers.pkg.tar.zst"
log "Refreshing pacman databases"
in_chroot "pacman --config $PACCONF -Sy"
in_chroot "pacman --config $PACCONF -U $PACOPTS /tmp/headers.pkg.tar.zst"
in_chroot "pacman --config $PACCONF -S $PACOPTS dkms"

# "name version" (not -Qq name-only): this tool upgrades an ALREADY
# installed driver, so name-only diffing would never notice a version bump
# on a package that was already there (nvidia-utils before AND after).
in_chroot "pacman -Q" | LC_ALL=C sort > "$WORKDIR/before.txt"

# Snapshot what the CURRENTLY installed driver packages own, before
# upgrading them. NVIDIA's shared libraries embed the full version in their
# filename (e.g. libnvidia-glcore.so.610.43.03) — a normal file-copy upgrade
# never removes the old version's same-role file under its different name,
# so without this the previous version's files would just accumulate on
# every update. Diffed against the new payload after the copy (step 7) to
# find and remove what's now stale.
OLD_DRIVER_RAW="$WORKDIR/old-driver-files.raw"
: > "$OLD_DRIVER_RAW"
for pkg in "${PKG_NAMES[@]}"; do
  in_chroot "pacman -Qlq $pkg 2>/dev/null" >> "$OLD_DRIVER_RAW" || true
done
OLD_DRIVER_FILES="$WORKDIR/old-driver-files.rel"
sed 's|^/||' "$OLD_DRIVER_RAW" | sort -u > "$OLD_DRIVER_FILES"

log "Installing pinned driver packages (compiles the module)"
rm -rf "$MERGED/tmp/nvpkgs"; mkdir -p "$MERGED/tmp/nvpkgs"
for f in "${PKG_FILES[@]}"; do cp "$WORKDIR/pkgs/$f" "$MERGED/tmp/nvpkgs/"; done
in_chroot "pacman --config $PACCONF -U $PACOPTS /tmp/nvpkgs/*.pkg.tar.zst" \
  || die "pacman -U failed. If it was a signature/keyring error, rerun with --skip-sigcheck."

# Check $UPPER specifically, not the merged view: lowerdir is the live
# system, which already has an nvidia.ko from the version being replaced —
# compgen against $MERGED would find that stale file and wrongly conclude
# the build succeeded even when nothing was rebuilt this session.
if ! compgen -G "$UPPER/usr/lib/modules/$KVER/updates/dkms/nvidia.ko*" >/dev/null; then
  log "DKMS didn't (re)build the module this run — forcing"
  # DKMS tracks only the upstream version string (e.g. "610.43.03"), not the
  # Arch pkgrel — a pkgrel-only rebuild (-4 -> -5) looks like "no change" to
  # DKMS, which then refuses to reinstall ("not newer... override with
  # --force"). This is a known DKMS limitation, not specific to this script.
  in_chroot "dkms install --force --no-depmod nvidia/$NV_PKGVER -k $KVER" \
    || die "Forced dkms install failed (check output above)"
  compgen -G "$UPPER/usr/lib/modules/$KVER/updates/dkms/nvidia.ko*" >/dev/null \
    || die "nvidia module failed to build for $KVER (check output above)"
fi
NVIDIA_VER="$(in_chroot "pacman -Q nvidia-utils" | awk '{print $2}')"
[[ "$NVIDIA_VER" == "$DRIVER_VERSION" ]] || die "Chroot has nvidia-utils $NVIDIA_VER but $DRIVER_VERSION was pinned"
log "Built nvidia-open $NVIDIA_VER for $KVER"

in_chroot "pacman -Q" | LC_ALL=C sort > "$WORKDIR/after.txt"

# --------------------------------------------- step 6: compute the payload
step "Computing payload file list"

BUILD_ONLY_RE='^(dkms|nvidia-open-dkms|patch|gcc|gcc-libs|make|binutils|libisl|libmpc|mpfr|pahole|python-setuptools|linux-neptune.*-headers|.*-headers)$'
# comm on full "name version" lines catches upgrades of already-installed
# packages too, not just brand-new ones; awk strips back to just the name.
mapfile -t NEW_PKGS < <(LC_ALL=C comm -13 "$WORKDIR/before.txt" "$WORKDIR/after.txt" | awk '{print $1}' | grep -Ev "$BUILD_ONLY_RE")
[[ ${#NEW_PKGS[@]} -gt 0 ]] || die "Payload package list came out empty"
log "Payload packages: ${NEW_PKGS[*]}"

FILELIST="$WORKDIR/payload-files.txt"
: > "$FILELIST"
for pkg in "${NEW_PKGS[@]}"; do in_chroot "pacman -Qlq $pkg" >> "$FILELIST"; done

sed 's|^/||' "$FILELIST" > "$FILELIST.rel"

# Space check BEFORE touching the real system: SteamOS rootfs partitions are
# small, and this tool (unlike the USB image builder) installs onto the
# ALREADY-USED live one — running out of space mid-copy leaves the driver
# half-written. Fail here, before rootfs is even unlocked, instead of there.
#
# This sizes the NET delta, not the whole package: on an upgrade (the usual
# case for this tool) most files already exist on the live system at the
# same path — pacman itself reports this as e.g. "Net Upgrade Size: 132
# MiB" while "Total Installed Size" can be 10x that. Summing full file
# sizes as if this were a first-time install way overestimates what's
# actually needed and can block a perfectly safe upgrade.
net_new_mb() {  # <source-root> <dest-root-prefix> <relpath-list>
  local src="$1" dest="$2" list="$3" p new_sz old_sz delta sum=0
  while IFS= read -r p; do
    [[ -f "$src/$p" || -L "$src/$p" ]] || continue
    new_sz=$(stat -c%s "$src/$p" 2>/dev/null || echo 0)
    old_sz=$(stat -c%s "$dest/$p" 2>/dev/null || echo 0)
    delta=$(( new_sz - old_sz ))
    (( delta > 0 )) && sum=$(( sum + delta ))
  done < "$list"
  echo $(( (sum + 1048575) / 1048576 ))
}

PAYLOAD_MB="$(net_new_mb "$MERGED" "" "$FILELIST.rel")"
[[ "$PAYLOAD_MB" =~ ^[0-9]+$ ]] || die "Could not size the payload"
# Modules: same idea, but simpler — just the total size of what got built,
# uncommon for kernel modules to be large enough to need the same treatment.
MODULES_MB="$(du -sm "$UPPER/usr/lib/modules/$KVER/updates" 2>/dev/null | cut -f1)"
MODULES_MB="${MODULES_MB:-0}"

# Best-effort: if this rootfs's partition was ever grown at the GPT level
# without its btrfs filesystem actually being told to grow into it (e.g. an
# older run of steamos-nvidia-installer.sh's USB repair enlarged rootfs-A/B
# but this happens to be the slot that never got its filesystem resized),
# fill it now. Growing (never shrinking) a live, mounted btrfs filesystem is
# a normal, safe, online operation — no rootfs unlock needed for this.
if btrfs filesystem resize max / >/dev/null 2>&1; then
  log "Grew this rootfs's filesystem to fill its partition (if it wasn't already)"
fi

AVAIL_MB="$(df -m --output=avail / | tail -1 | tr -d ' ')"
log "Net new ≈ ${PAYLOAD_MB} MB files + ${MODULES_MB} MB modules; this system's rootfs has ${AVAIL_MB} MB free"
if (( (PAYLOAD_MB + MODULES_MB) * 110 / 100 > AVAIL_MB )); then
  die "Not enough free space on this system's rootfs (need ~$((PAYLOAD_MB + MODULES_MB)) MB + margin, have ${AVAIL_MB} MB). Free up space first."
fi

# --------------------------------------- step 7: install into the live system
step "Installing into the running system (brief rootfs unlock)"

# The only part of this run that touches the real, booted rootfs — a file
# copy plus depmod, not a build, so the unlocked window is short. Nothing
# here ever references any device path other than the one we're already
# booted from: no other disk or partition (Windows or otherwise) is opened.
log "Unlocking rootfs (steamos-readonly disable)"
if command -v steamos-readonly >/dev/null; then
  steamos-readonly disable
else
  btrfs property set / ro false
fi
ROOTFS_DISABLED=1

log "Copying driver payload into the running system"
# --checksum: compare actual content, not size+mtime. mtimes always differ
# (files were just extracted by this build), so without --checksum rsync
# would rewrite even byte-identical files (e.g. GSP firmware unchanged
# across a pkgrel-only bump) — needless writes that also each need a brief
# transient 2x-the-file's-size of headroom (temp copy + original, until the
# atomic rename). Skipping truly-unchanged files avoids that entirely,
# which matters a lot on a rootfs partition this tight on space.
rsync -a --checksum --files-from="$FILELIST.rel" "$MERGED/" /
rsync -a --checksum "$UPPER/usr/lib/modules/$KVER/updates" "/usr/lib/modules/$KVER/"

log "Registering payload packages in the pacman db"
for pkg in "${NEW_PKGS[@]}"; do
  for ENTRY in "$UPPER/usr/lib/holo/pacmandb/local/$pkg"-[0-9]*; do
    [[ -d "$ENTRY" ]] || continue
    NEWNAME="$(basename "$ENTRY")"
    # This is an upgrade, not a first-time install: the OLD version's db
    # entry is still sitting on the live system. Leaving both around makes
    # pacman see two directories for the same package name ("duplicated
    # database entry") and refuse to report it at all — remove the old one.
    for OLD in /usr/lib/holo/pacmandb/local/"$pkg"-[0-9]*; do
      [[ -d "$OLD" && "$(basename "$OLD")" != "$NEWNAME" ]] && rm -rf "$OLD"
    done
    rsync -a "$ENTRY" /usr/lib/holo/pacmandb/local/
    break
  done
done

log "Cleaning up stale files from the previous driver version"
STALE="$WORKDIR/stale-driver-files.rel"
comm -23 "$OLD_DRIVER_FILES" <(sort -u "$FILELIST.rel") > "$STALE"
STALE_COUNT=0
STALE_DIRS=()
while IFS= read -r p; do
  [[ -n "$p" ]] || continue
  [[ -e "/$p" || -L "/$p" ]] || continue
  # Belt and suspenders on a live, running system: only remove it if no
  # currently-installed package (the new one included) still owns it.
  pacman -Qqo "/$p" >/dev/null 2>&1 && continue
  # pacman tracks directories as owned entries too (e.g. a version-numbered
  # dir like /usr/lib/firmware/nvidia/595.71.05/), not just regular files --
  # `rm -f` refuses those ("Is a directory") and, under `set -e`, that
  # failure used to kill the whole script mid-cleanup, after the real
  # install had already succeeded. Defer directories to a second pass below.
  if [[ -d "/$p" && ! -L "/$p" ]]; then
    STALE_DIRS+=("/$p")
    continue
  fi
  rm -f "/$p"
  STALE_COUNT=$(( STALE_COUNT + 1 ))
done < "$STALE"
# rmdir only succeeds once a directory is truly empty -- never a risk of
# deleting a file that isn't itself confirmed stale. Deepest paths first so
# a nested stale directory empties out before its parent is attempted.
if (( ${#STALE_DIRS[@]} > 0 )); then
  while IFS= read -r d; do
    rmdir "$d" 2>/dev/null && STALE_COUNT=$(( STALE_COUNT + 1 ))
  done < <(printf '%s\n' "${STALE_DIRS[@]}" | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2-)
fi
(( STALE_COUNT > 0 )) && log "Removed $STALE_COUNT stale file(s)/dir(s) left over from the previous driver version"

log "Running depmod + ldconfig"
depmod "$KVER"
ldconfig

mkdir -p /etc/modprobe.d
cat > /etc/modprobe.d/99-nvidia-patch.conf <<'EOF'
# Added by steamos-nvidia-installer / steamos-nvidia-update
blacklist nouveau
options nouveau modeset=0
options nvidia-drm modeset=1 fbdev=1
options nvidia NVreg_PreserveVideoMemoryAllocations=1
EOF
systemctl enable nvidia-suspend nvidia-resume nvidia-hibernate 2>/dev/null \
  || warn "Could not enable nvidia power services (non-fatal)"

# Keep the OS-update self-heal machinery (if present) in sync, so the NEXT
# Steam OS update doesn't silently reinstall the driver version you just
# moved away from.
if [[ -f /usr/lib/steamos-nvidia/driver.conf ]]; then
  log "Updating self-heal driver.conf to match this driver"
  cat > /usr/lib/steamos-nvidia/driver.conf <<EOF
# Written by steamos-nvidia-update.sh.
DRIVER_SPEC="$DRIVER_SPEC"
DRIVER_VERSION="$DRIVER_VERSION"
PKG_URLS="$PKG_URLS"
EOF
  chmod 644 /usr/lib/steamos-nvidia/driver.conf
fi

log "Locking rootfs (steamos-readonly enable)"
if command -v steamos-readonly >/dev/null; then
  steamos-readonly enable
else
  btrfs property set / ro true
fi
ROOTFS_DISABLED=0

# ----------------------------------------------- step 8: final verification
step "Final verification"

compgen -G "/usr/lib/modules/$KVER/updates/dkms/nvidia.ko*" >/dev/null \
  || die "Post-install check failed: nvidia.ko missing from the running system"
grep -q 'blacklist nouveau' /etc/modprobe.d/99-nvidia-patch.conf \
  || die "Post-install check failed: modprobe conf missing/empty"
[[ "$(pacman -Q nvidia-utils | awk '{print $2}')" == "$DRIVER_VERSION" ]] \
  || die "Post-install check failed: pacman db doesn't show nvidia-utils $DRIVER_VERSION"
log "All checks passed."

DONE_MSG="nvidia-open $NVIDIA_VER installed for $KVER.\n\nReboot to load the new module. Games, saves, Steam login and Decky were never touched; no other disk or partition was touched."
if [[ $GUI -eq 1 ]]; then
  zenity --info --no-wrap --title "Done" --text "$DONE_MSG" 2>/dev/null || true
fi
log "DONE — nvidia-open $NVIDIA_VER installed for $KVER."
log "Reboot to load the new module. Games, saves, Steam login, Decky, and every other disk/partition: untouched."
