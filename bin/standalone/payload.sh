#!/bin/bash
# Internal payload worker. Destination must be a separate Btrfs mount.
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
SKIP_SIG=0
WORKDIR=""
TARGET=""
KVER=""
PINS=""
BUILD_ETC=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)        TARGET="${2:?}"; shift ;;
    --build-etc)     BUILD_ETC="${2:?}"; shift ;;
    --pins)          PINS="${2:?--pins needs an argument}"; shift ;;
    --kernel)        KVER="${2:?}"; shift ;;
    --driver)        DRIVER_SPEC="${2:?--driver needs an argument}"; shift ;;
    --list)          LIST_ONLY=1 ;;
    --force|-y|--yes) ;;
    --skip-sigcheck) SKIP_SIG=1 ;;
    --workdir)       WORKDIR="${2:?--workdir needs an argument}"; shift ;;
    -h|--help)       echo 'Internal worker: use ../steamos-nvidia-update.sh'; exit 0 ;;
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
[[ $$ -eq 1 && ${DECKY_NVIDIA_BUILD_NS:-0} == 1 ]] || die "Payload must run in its private build process namespace"
[[ -n "$TARGET" && "$TARGET" != / && -n "$KVER" ]] || die "Internal worker needs a target and kernel"
TARGET="$(readlink -f "$TARGET")"
[[ "$TARGET" != / && "$(findmnt -n -o FSTYPE --mountpoint "$TARGET")" == btrfs ]] || die "Target is not a separate Btrfs mount"
[[ "$(findmnt -n -o MAJ:MIN --mountpoint "$TARGET")" != "$(findmnt -n -o MAJ:MIN /)" ]] || die "Target is the active root"
[[ "$KVER" =~ ^[a-zA-Z0-9._+-]+$ && -d "$TARGET/usr/lib/modules/$KVER" ]] || die "Invalid target kernel"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

for tool in curl pacman chroot mount mkfs.ext4 tar zstd python3 rsync depmod readelf findmnt df; do
  command -v "$tool" >/dev/null || die "Missing tool: $tool"
done
# /home must be its own filesystem: this is where the build workspace lives
# (a loopback file, never a raw partition) and it's also the concrete check
# that confirms we're on a normal SteamOS layout, not something unexpected.
mountpoint -q /home || die "/home is not a separate mounted filesystem — refusing to continue (unexpected disk layout)"

AVAIL_MB="$(df -m --output=avail /home | tail -1 | tr -d ' ')"
(( AVAIL_MB > 10000 )) || die "Not enough free space on /home: need ~10 GB, have ${AVAIL_MB} MB"

if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -qi 'enabled'; then
  warn "Secure Boot appears to be enabled — an unsigned DKMS nvidia module may fail to load after reboot. Disable Secure Boot or enroll a MOK key if the driver doesn't come up."
fi

# KVER is selected from the destination, never uname during an OS update.
CURRENT_VER="$(chroot "$TARGET" pacman -Q nvidia-utils 2>/dev/null | awk '{print $2}' || true)"
log "Target kernel: $KVER"
if [[ -n "$CURRENT_VER" ]]; then
  log "Currently installed nvidia-utils: $CURRENT_VER"
else
  log "No NVIDIA driver currently installed on this system."
fi

# --------------------------------------------------- step 2: choose version
step "Selecting driver version"

[[ "$DRIVER_SPEC" == latest || "$DRIVER_SPEC" =~ ^[0-9]+(\.[0-9]+)*(-[0-9]+(\.[0-9]+)*)?$ ]] \
  || die "--driver takes 'latest' or a version prefix like 580 / 580.105.08 / 580.105.08-4"
log "Selected: $DRIVER_SPEC"

# All workspaces are new children of root-owned, private transaction directories.
WORKDIR="$(readlink -m "$WORKDIR")"
case "$WORKDIR" in
  /home/.decky-nvidia-update/prepare-*/build|/home/.decky-nvidia-repair.*/build) ;;
  *) die "Unexpected private workspace" ;;
esac
[[ ! -e "$WORKDIR" && ! -L "$WORKDIR" && ! -e "$WORKDIR.img" && ! -L "$WORKDIR.img" ]] || die "Workspace already exists"
UPPER="$WORKDIR/upper"
OVLWORK="$WORKDIR/ovlwork"
MERGED="$WORKDIR/merged"
WORKIMG="$WORKDIR.img"   # ext4 loopback FILE on /home — never a raw partition.
                          # SteamOS /home is casefold ext4, which overlayfs
                          # rejects as an upperdir, hence a plain ext4 file.

# build_payload owns cleanup. Exiting this PID namespace terminates helpers
# and releases all of its mounts, including references to the Btrfs lowerdir.
# The parent removes the backing file only after checking loop teardown.

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
      # `|| true` is load-bearing under `set -e`: without it, the very
      # first failed attempt (e.g. archlinux.org's occasional plain 502)
      # exits the whole script right here with curl's own raw exit code —
      # before this loop ever gets to retry, defeating the retry entirely.
      json="$(curl -sfL "https://archlinux.org/packages/search/json/?name=$pkg" 2>/dev/null)" || true
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
      # Same `set -e` trap as the "latest" branch above — `|| true` keeps a
      # single failed attempt from killing the script before the retry
      # loop gets a chance to run again.
      html="$(curl -sfL "$ARCHIVE_URL/${pkg:0:1}/$pkg/" 2>/dev/null)" || true
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
  # A custom build (see pin_pkg()) is already sitting on local disk — copy
  # instead of curl, no network/progress display needed.
  if [[ "$url" == file://* ]]; then
    cp "${url#file://}" "$dest"
    return $?
  fi
  # Purely cosmetic (falls back to a plain MB counter below if empty) —
  # `|| true` so a network hiccup on this HEAD request can't take down the
  # whole run under `set -e`/`pipefail` over what's just a progress display.
  total="$(curl -sIL "$url" 2>/dev/null | tr -d '\r' \
    | awk 'BEGIN{IGNORECASE=1} /^content-length:/{v=$2} END{print v}')" || true
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

if [[ -n "$PINS" ]]; then
  pins=$(python3 -I "$SCRIPT_DIR/phase.py" package-pins "$PINS")
  eval "$pins"
else
pin_pkg nvidia-utils "$DRIVER_SPEC"
DRIVER_VERSION="$PIN_VER"; NV_PKGVER="${PIN_VER%-*}"

log "Driver pinned: nvidia-open $DRIVER_VERSION"
fetch_pins

COMPANION_SPEC="$DRIVER_SPEC"
[[ "$COMPANION_SPEC" == latest ]] || COMPANION_SPEC="$NV_PKGVER"
for pkg in nvidia-open-dkms lib32-nvidia-utils; do
  pin_pkg "$pkg" "$COMPANION_SPEC"
  [[ "$PIN_VER" == "$NV_PKGVER"-* ]] \
    || die "Version skew: $pkg is $PIN_VER but nvidia-utils is $DRIVER_VERSION (mirror mid-update?) — retry in an hour"
done
fi
fetch_pins
ARCH_ONLY_DEPS=" egl-wayland2 "
while read -r dep; do
  [[ -n "$dep" && "$ARCH_ONLY_DEPS" == *" $dep "* ]] || continue
  [[ " ${PKG_NAMES[*]} " != *" $dep "* ]] || continue
  log "  $DRIVER_VERSION also needs $dep, which Valve's repo predates"
  pin_pkg "$dep" latest
done < <(tar -xOf "$WORKDIR/pkgs/${PKG_FILES[0]}" .PKGINFO \
         | awk '$1 == "depend" { print $3 }' | sed 's/[<>=].*//')
fetch_pins

# ------------------------------------------ step 4: glibc compatibility
step "Checking glibc compatibility"
python3 -I "$SCRIPT_DIR/compat.py" "$NV_PKGVER"

IMG_GLIBC="$(chroot "$TARGET" pacman -Q glibc | awk '{print $2}' | grep -oE '^[0-9]+\.[0-9]+')"
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
step "Building the driver (overlay chroot + dkms, this can take a few minutes)"

# Build against the prepared target. All compilation residue stays in $UPPER.
mount -t overlay overlay -o "index=off,lowerdir=$TARGET,upperdir=$UPPER,workdir=$OVLWORK" "$MERGED"
# A driver change uses the host's current repository and keyring settings.
# An OS repair defaults to the NEW target's settings and kernel repositories.
# These build inputs belong only in this disposable overlay, never the base.
BUILD_ETC=${BUILD_ETC:-"$TARGET/etc"}
[[ -f "$BUILD_ETC/pacman.conf" && ! -L "$BUILD_ETC/pacman.conf" && -d "$BUILD_ETC/pacman.d" && ! -L "$BUILD_ETC/pacman.d" ]] || die "Unsupported pacman build configuration"
[[ ! -L "$MERGED/etc" && ! -L "$MERGED/etc/pacman.d" ]] || die "Unsafe pacman build destination"
# Btrfs exposes compression as an xattr, but the ext4 build upper cannot set
# or remove it through overlayfs. Keep other metadata, including ACLs; an
# explicit xattr filter must also retain rsync's default system.* exclusion.
PACMAN_COPY=(rsync -aHAX --numeric-ids --filter='-x system.*' --filter='-x btrfs.compression')
rm -f "$MERGED/etc/pacman.conf"
"${PACMAN_COPY[@]}" "$BUILD_ETC/pacman.conf" "$MERGED/etc/pacman.conf"
mkdir -p "$MERGED/etc/pacman.d"
"${PACMAN_COPY[@]}" --delete "$BUILD_ETC/pacman.d/" "$MERGED/etc/pacman.d/"
mount -t proc proc "$MERGED/proc"
mount --rbind /sys "$MERGED/sys";  mount --make-rslave "$MERGED/sys"
mount --rbind /dev "$MERGED/dev";  mount --make-rslave "$MERGED/dev"
rm -f "$MERGED/etc/resolv.conf"
cp -L /etc/resolv.conf "$MERGED/etc/resolv.conf"

in_chroot() { python3 -I "$SCRIPT_DIR/initcpio_compat.py" "$MERGED" /bin/bash -c "$*"; }

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
PACDB="$TARGET/usr/lib/holo/pacmandb/local"
KPKG_NAME="$(chroot "$TARGET" pacman -Qqo "/usr/lib/modules/$KVER/pkgbase")"
[[ "$KPKG_NAME" =~ ^linux-neptune[a-zA-Z0-9_-]*$ ]] || die "Unsupported kernel package"
KPKG_VERREL="$(chroot "$TARGET" pacman -Q "$KPKG_NAME" | awk '{print $2}')"

JUPITER_REPO="$(awk -F'[][]' '/^\[jupiter-/{print $2; exit}' "$MERGED/etc/pacman.conf")"
MIRROR="$(awk '/^Server/{print $3; exit}' "$MERGED/etc/pacman.d/mirrorlist")"
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
mapfile -t NEW_PKGS < <({
  LC_ALL=C comm -13 "$WORKDIR/before.txt" "$WORKDIR/after.txt" | awk '{print $1}' | grep -Ev "$BUILD_ONLY_RE"
  printf '%s\n' "${PKG_NAMES[@]}" | grep -v '^nvidia-open-dkms$'
} | LC_ALL=C sort -u)

[[ ${#NEW_PKGS[@]} -gt 0 ]] || die "Payload package list came out empty"
log "Payload packages: ${NEW_PKGS[*]}"

FILELIST="$WORKDIR/payload-files.txt"
: > "$FILELIST"
for pkg in "${NEW_PKGS[@]}"; do in_chroot "pacman -Qlq $pkg" >> "$FILELIST"; done

sed 's|^/||' "$FILELIST" > "$FILELIST.rel"
python3 -I "$SCRIPT_DIR/phase.py" trim "$TARGET" "$FILELIST.rel"
# Never export build-only repository/keyring inputs through a dependency's
# package file list. A driver payload must not replace the package manager.
if grep -Eq '^etc/pacman(\.conf(/|$)|\.d(/|$))' "$FILELIST.rel"; then
  die "Driver payload unexpectedly includes pacman configuration"
fi

# The destination is disposable or an inactive, disabled OS slot. Reclaim
# obsolete files here before copying; never do this on the running root.
step "Installing into the prepared system"
python3 "$SCRIPT_DIR/driver.py" prune "$TARGET" "$FILELIST.rel"
rsync -a --checksum --files-from="$FILELIST.rel" "$MERGED/" "$TARGET/"
rsync -a --checksum "$UPPER/usr/lib/modules/$KVER/updates" "$TARGET/usr/lib/modules/$KVER/"
for pkg in "${NEW_PKGS[@]}"; do
  for ENTRY in "$MERGED/usr/lib/holo/pacmandb/local/$pkg"-[0-9]*; do
    [[ -d "$ENTRY" ]] || continue
    NEWNAME="$(basename "$ENTRY")"
    for OLD in "$TARGET/usr/lib/holo/pacmandb/local/$pkg"-[0-9]*; do
      [[ -d "$OLD" && "$(basename "$OLD")" != "$NEWNAME" ]] && rm -rf -- "$OLD"
    done
    rsync -a "$ENTRY" "$TARGET/usr/lib/holo/pacmandb/local/"
    break
  done
done
chroot "$TARGET" depmod "$KVER"
chroot "$TARGET" ldconfig
python3 "$SCRIPT_DIR/driver.py" record "$TARGET" "$DRIVER_VERSION" "$KVER" "$PKG_URLS"
step "Verifying prepared modules and libraries"
check_driver "$TARGET" "$KVER" "$NV_PKGVER"
require_space "$TARGET" 256
log "Prepared $DRIVER_VERSION for $KVER; the running driver is unchanged."
