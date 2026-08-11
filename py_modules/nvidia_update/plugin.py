import asyncio
import html
import json
import os
import re
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import decky

# The plugin ships its own copy of the CLI tool (bin/steamos-nvidia-update.sh)
# so the exact same script can be run by hand over SSH/terminal or driven
# from this UI — one mechanism, two ways to trigger it.
SCRIPT_PATH = Path(decky.DECKY_PLUGIN_DIR) / "bin" / "steamos-nvidia-update.sh"

# Full run output, persisted — so a failure can be read with `cat`/`tail -f`
# instead of screenshotting the UI's scrollback. Overwritten each run.
LOG_FILE = Path(decky.DECKY_PLUGIN_LOG_DIR) / "nvidia-update-run.log"

SETTINGS_FILE = Path(decky.DECKY_PLUGIN_SETTINGS_DIR) / "settings.json"
DEFAULT_SETTINGS: Dict[str, Any] = {
    "show_build_numbers": False,
    # Which NVIDIA channel the version dropdown defaults to when no manual
    # pick has been made yet — replaces the old "treat beta as latest"
    # toggle with an explicit choice among the three real channels, or
    # "all" (default) for "whatever is newest, regardless of channel".
    "default_channel": "all",
    "channel_filters_expanded": True,
}


def _clean_env() -> Dict[str, str]:
    """Decky Loader's own backend process runs with LD_LIBRARY_PATH pointed
    at its bundled libs; that leaks into every subprocess we spawn and makes
    system binaries (bash, pacman, ...) load the wrong shared libs — e.g.
    'bash: symbol lookup error: undefined symbol: rl_trim_arg_from_keyseq'
    from bash picking up Decky's bundled libreadline instead of the
    system's. Stripping it before exec is the standard fix."""
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    return env

STEP_RE = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
VERSION_SPLIT_RE = re.compile(r"(\d+)")

STABLE_REPOS = ("core", "extra", "multilib")


def _version_key(v: str):
    """Natural-sort key ('610.57.04-1' > '610.43.03-5'), digit runs compared
    numerically rather than lexicographically."""
    return [int(p) if p.isdigit() else p for p in VERSION_SPLIT_RE.split(v)]

# NVIDIA's own (undocumented, unofficial) driver-lookup API — the same one
# backing the search widget at nvidia.com/en-us/drivers/results/. psid/pfid
# identify a product series/family in NVIDIA's system; these values were
# obtained from a real Linux driver search and return the general desktop
# GeForce/RTX Linux driver lineage, which is what every modern desktop
# NVIDIA GPU actually uses (Linux driver builds aren't per-GPU-model) — not
# tied to any specific card. osID=12 is Linux 64-bit. No official docs exist
# for this endpoint; it could change or start blocking scripted requests
# without notice. Every caller of this treats that as a normal, non-fatal
# failure (empty result), same as the archlinux.org lookups above.
NVIDIA_DRIVER_LOOKUP_URL = (
    "https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/"
    "AjaxDriverService.php?func=DriverManualLookup&psid=131&pfid=1068&osID=12"
    "&languageCode=1033&isWHQL=0&beta=null&dltype=-1&dch=0&upCRD=null&qnf=0"
    "&ctk=null&sort1=0&numberOfResults=200"
)


def _nvidia_notes_to_lines(raw_encoded: str) -> List[str]:
    """NVIDIA's ReleaseNotes field is URL-encoded HTML (<ul><li>...</li></ul>,
    with <a href> links and stray <br> tags) — turn it into plain text lines
    for display, same shape as the existing scrollback log."""
    if not raw_encoded:
        return []
    text = urllib.parse.unquote(raw_encoded)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<a[^>]*>(.*?)</a>", r"\1", text)
    items = re.findall(r"(?is)<li[^>]*>(.*?)</li>", text) or [text]
    lines = []
    for item in items:
        item = re.sub(r"(?i)<[^>]+>", "", item)
        item = html.unescape(item)
        item = re.sub(r"\s+", " ", item).strip()
        if item:
            lines.append(item)
    return lines

DEFAULT_STATE: Dict[str, Any] = {
    "running": False,
    "step": 0,
    "total_steps": 8,
    "message": "",
    "log": [],
    "done": False,
    "success": None,
    "error": None,
    # Set only for specific, recognized failures (currently just the Arch
    # server being unreachable) so the frontend can show a proper localized
    # message instead of relaying the script's raw (English-only) text.
    # None for anything else — `error` is still always the human-readable
    # fallback either way.
    "error_code": None,
    "driver_spec": None,
}

# Marker die() prefixes the script's own message with on specific, known
# failure kinds — matched below to set error_code. Anything else just
# surfaces as plain (English, untranslated) text via `error`, same as before.
ERROR_CODE_MARKERS = {
    "ERR_ARCH_SERVER_UNREACHABLE": "arch_server_unreachable",
}


class Plugin:
    # Mutable per-instance state (one update can run at a time).
    _state: Dict[str, Any] = dict(DEFAULT_STATE)
    _lock = asyncio.Lock()
    _task: Optional[asyncio.Task] = None  # keeps the update task alive

    # ── Version discovery ────────────────────────────────────────────────

    async def get_current_version(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pacman", "-Q", "nvidia-utils",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_clean_env(),
            )
            out, _ = await proc.communicate()
            parts = out.decode(errors="replace").split()
            return parts[1] if len(parts) > 1 else ""
        except Exception as e:
            decky.logger.error(f"[get_current_version] {e}")
            return ""

    async def get_driver_repo_info(self) -> Dict[str, Any]:
        """One archlinux.org lookup covering everything the UI needs to
        reason about 'latest' vs beta:
          - stable: newest pkgver-pkgrel currently in core/extra/multilib
          - with_testing: newest overall, counting *-testing repos too
          - testing_versions: pkgver-pkgrel currently sitting in a testing
            repo (not yet promoted to stable) — used to label the picker
        Archive.archlinux.org (used by --list) mirrors every upload,
        including testing builds not yet promoted to the stable repo —
        this is what lets the UI tell them apart instead of just showing
        a raw 'newer' number that may not actually be the current release."""
        empty = {"stable": "", "with_testing": "", "testing_versions": []}
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sfL",
                "https://archlinux.org/packages/search/json/?name=nvidia-utils",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_clean_env(),
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return empty
            data = json.loads(out.decode(errors="replace"))
            results = [
                p for p in data.get("results", [])
                if p.get("arch") == "x86_64" and p.get("repo")
            ]

            def ver(p: Dict[str, Any]) -> str:
                return f"{p['pkgver']}-{p['pkgrel']}"

            stable = [ver(p) for p in results if p["repo"] in STABLE_REPOS]
            testing = [ver(p) for p in results if "testing" in p["repo"]]
            combined = stable + testing
            return {
                "stable": max(stable, key=_version_key) if stable else "",
                "with_testing": max(combined, key=_version_key) if combined else "",
                "testing_versions": testing,
            }
        except Exception as e:
            decky.logger.error(f"[get_driver_repo_info] {e}")
            return empty

    async def get_nvidia_branch_info(self) -> Dict[str, Any]:
        """NVIDIA's own driver-lookup service, queried live on every call (not
        cached/baked in) — classifies EVERY release it returns (in practice,
        as much history as it's willing to give per request — around 40
        entries, spanning roughly the last year and a half) as Recommended
        (production), New Feature Branch, or Beta, something Arch's own
        repos have no concept of (they only distinguish stable vs testing).
        This is applied across the WHOLE Arch-available version list on the
        frontend, not just a single 'latest per category' pick — a version
        Arch has that isn't in this map is simply left unclassified (shown
        regardless of filter state, since we have no basis to hide it).
        Best-effort: this is an unofficial endpoint with no SLA, so any
        failure here degrades to an empty map rather than affecting
        anything else in the UI."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sfL", NVIDIA_DRIVER_LOOKUP_URL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_clean_env(),
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return {}
            data = json.loads(out.decode(errors="replace"))
            entries = [e["downloadInfo"] for e in data.get("IDS", []) if "downloadInfo" in e]

            versions: Dict[str, Any] = {}
            for e in entries:
                v = e.get("DisplayVersion")
                if not v:
                    continue
                if e.get("IsRecommended") == "1":
                    category = "recommended"
                elif e.get("IsFeaturePreview") == "1":
                    category = "nfb"
                elif e.get("IsBeta") == "1":
                    category = "beta"
                else:
                    continue
                versions[v] = {
                    "category": category,
                    "date": e.get("ReleaseDateTime", ""),
                    "notes": _nvidia_notes_to_lines(e.get("ReleaseNotes", "")),
                }
            decky.logger.info(
                f"[get_nvidia_branch_info] fetched {len(entries)} NVIDIA entries, "
                f"classified {len(versions)}"
            )
            return versions
        except Exception as e:
            decky.logger.error(f"[get_nvidia_branch_info] {e}")
            return {}

    async def list_driver_versions(self) -> List[str]:
        """Recent nvidia-utils versions, newest first (script prints oldest→newest)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(SCRIPT_PATH), "--list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_clean_env(),
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                decky.logger.error(f"[list_driver_versions] script failed: {err.decode(errors='replace')}")
                return []
            versions = [
                ANSI_RE.sub("", line).strip()
                for line in out.decode(errors="replace").splitlines()
            ]
            versions = [v for v in versions if re.match(r"^\d", v)]
            versions.reverse()
            return versions
        except Exception as e:
            decky.logger.error(f"[list_driver_versions] {e}\n{traceback.format_exc()}")
            return []

    async def list_driver_versions_detailed(self) -> List[str]:
        """Same archive listing as list_driver_versions(), but keeps the
        exact pkgrel (e.g. '610.43.03-5' instead of '610.43.03') — lets the
        UI optionally show/select an exact historical build rather than
        always 'the newest build of this pkgver'."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sfL",
                "https://archive.archlinux.org/packages/n/nvidia-utils/",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_clean_env(),
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return []
            # [^"<\s]*, not [^"<]* — unlike grep (line-oriented), Python's re
            # on the whole multi-line blob would let a bare [^"<]* span
            # across newlines and swallow far more than one filename.
            matches = re.findall(
                r"nvidia-utils-(\d[^\"<\s]*)-x86_64\.pkg\.tar\.zst",
                out.decode(errors="replace"),
            )
            # Cap by DISTINCT PKGVER (matching list_driver_versions()'s own
            # "sort -uV | tail -15"), not by raw build count — a pkgver with
            # many pkgrel rebuilds (e.g. a long-supported 580.x) would
            # otherwise fill the whole cap by itself and push older, less-
            # rebuilt pkgvers (e.g. 575.x) out of the list entirely, even
            # though they're still within the top-15 distinct versions.
            by_pkgver: Dict[str, List[str]] = {}
            for m in set(matches):
                base = re.sub(r"-\d+$", "", m)
                by_pkgver.setdefault(base, []).append(m)
            top_pkgvers = sorted(by_pkgver.keys(), key=_version_key, reverse=True)[:15]
            result: List[str] = []
            for pv in top_pkgvers:
                result.extend(sorted(by_pkgver[pv], key=_version_key, reverse=True))
            return result
        except Exception as e:
            decky.logger.error(f"[list_driver_versions_detailed] {e}\n{traceback.format_exc()}")
            return []

    # ── Update lifecycle ─────────────────────────────────────────────────

    async def get_status(self) -> Dict[str, Any]:
        return dict(Plugin._state)

    async def get_log_path(self) -> str:
        return str(LOG_FILE)

    # ── Settings (persisted across sessions) ────────────────────────────

    async def get_settings(self) -> Dict[str, Any]:
        try:
            if SETTINGS_FILE.is_file():
                saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                return {**DEFAULT_SETTINGS, **saved}
        except Exception as e:
            decky.logger.error(f"[get_settings] {e}")
        return dict(DEFAULT_SETTINGS)

    async def save_settings(self, settings: Dict[str, Any]) -> bool:
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            merged = {**DEFAULT_SETTINGS, **settings}
            SETTINGS_FILE.write_text(json.dumps(merged), encoding="utf-8")
            return True
        except Exception as e:
            decky.logger.error(f"[save_settings] {e}")
            return False

    async def start_update(
        self, driver_spec: str, force: bool = False
    ) -> bool:
        if Plugin._state["running"]:
            return False
        Plugin._state = dict(DEFAULT_STATE)
        Plugin._state["running"] = True
        Plugin._state["driver_spec"] = driver_spec
        Plugin._state["message"] = "Starting…"
        Plugin._task = asyncio.create_task(self._run(driver_spec, force))
        return True

    async def _run(self, driver_spec: str, force: bool) -> None:
        args = ["bash", str(SCRIPT_PATH), "--driver", driver_spec, "-y"]
        if force:
            args.append("--force")
        decky.logger.info(f"[nvidia-update] launching: {' '.join(args)}")

        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(LOG_FILE, "w", encoding="utf-8") as logf:
                logf.write(
                    f"=== nvidia-update {datetime.now().isoformat(timespec='seconds')} "
                    f"— driver={driver_spec} force={force} ===\n"
                )
                logf.flush()

                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=_clean_env(),
                )
                assert proc.stdout is not None
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = ANSI_RE.sub("", raw.decode(errors="replace")).rstrip()
                    if not line:
                        continue

                    logf.write(line + "\n")
                    logf.flush()

                    Plugin._state["log"].append(line)
                    Plugin._state["log"] = Plugin._state["log"][-200:]

                    # `message` only updates on an actual step transition, so
                    # the UI shows a stable "what this step is doing" line
                    # instead of flickering through every noisy line in
                    # between (download percentages, raw pacman output...).
                    # Those still land in the full scrollable `log` above.
                    m = STEP_RE.match(line)
                    if m:
                        Plugin._state["step"] = int(m.group(1))
                        Plugin._state["total_steps"] = int(m.group(2))
                        Plugin._state["message"] = m.group(3)

                    await decky.emit("nvidia_update_progress", dict(Plugin._state))

                rc = await proc.wait()
                logf.write(f"=== exited with code {rc} ===\n")

            Plugin._state["running"] = False
            Plugin._state["done"] = True
            Plugin._state["success"] = rc == 0
            if rc != 0:
                # die() in the script always prints "[fail] <reason>" right
                # before exiting — surfacing that exact reason (e.g. "Could
                # not reach archlinux.org...") in the prominent error box
                # instead of a generic "exited with code 1" is the whole
                # point: the user shouldn't have to open the log just to
                # find out it's a transient server issue worth retrying.
                fail_lines = [l for l in Plugin._state["log"] if l.startswith("[fail]")]
                reason = fail_lines[-1][len("[fail] "):] if fail_lines else ""
                # A recognized failure gets a proper localized message on the
                # frontend (error_code) instead of relaying this raw,
                # English-only script text — the marker itself is stripped
                # either way so it never leaks into what's displayed.
                Plugin._state["error_code"] = None
                for marker, code in ERROR_CODE_MARKERS.items():
                    if marker in reason:
                        Plugin._state["error_code"] = code
                        reason = reason.replace(marker, "").strip()
                        break
                Plugin._state["error"] = reason or f"Script exited with code {rc}."
            await decky.emit("nvidia_update_progress", dict(Plugin._state))
            decky.logger.info(f"[nvidia-update] finished rc={rc}")
        except Exception as e:
            decky.logger.error(f"[nvidia-update] exception: {e}\n{traceback.format_exc()}")
            Plugin._state["running"] = False
            Plugin._state["done"] = True
            Plugin._state["success"] = False
            Plugin._state["error"] = str(e)
            await decky.emit("nvidia_update_progress", dict(Plugin._state))

    async def reboot_system(self) -> bool:
        try:
            await asyncio.create_subprocess_exec("systemctl", "reboot", env=_clean_env())
            return True
        except Exception as e:
            decky.logger.error(f"[reboot_system] {e}")
            return False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _grow_rootfs_best_effort(self) -> None:
        """SteamOS system updates re-image whichever rootfs-A/B slot they
        land on from Valve's own payload, which resets THAT slot's btrfs
        filesystem back to its original ~5GiB size even when the underlying
        GPT partition is bigger (e.g. after steamos-nvidia-installer.sh's
        USB repair grew it). The driver itself survives updates fine — only
        the filesystem's reported size silently regresses, and stays that
        way until something regrows it. Growing (never shrinking) a live,
        mounted btrfs filesystem is a normal, safe online operation. Runs
        once per plugin load, which happens at least once per boot, so this
        doesn't depend on the user ever opening the update UI itself."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "btrfs", "filesystem", "resize", "max", "/",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=_clean_env(),
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                decky.logger.error(
                    f"[_grow_rootfs_best_effort] {err.decode(errors='replace').strip()}"
                )
        except Exception as e:
            decky.logger.error(f"[_grow_rootfs_best_effort] {e}")

    async def _main(self) -> None:
        decky.logger.info(f"decky-nvidia-update loaded (script: {SCRIPT_PATH})")
        await self._grow_rootfs_best_effort()

    async def _unload(self) -> None:
        decky.logger.info("decky-nvidia-update unloaded")

    async def _migration(self) -> None:
        pass
