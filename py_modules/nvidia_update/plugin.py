import asyncio
import json
import os
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import decky
from decky_plugin_toolkit import PluginUpdaterMixin, WhatsNewSeenMixin, OtherPluginsSeenMixin

from .catalog import fetch_catalog
from .custom_build import CustomBuildMixin
from .standalone import StandaloneMixin, current_log_path

# The plugin ships its own copy of the CLI tool (bin/steamos-nvidia-update.sh)
# so the exact same script can be run by hand over SSH/terminal or driven
# from this UI — one mechanism, two ways to trigger it.
SCRIPT_PATH = Path(decky.DECKY_PLUGIN_DIR) / "bin" / "steamos-nvidia-update.sh"

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
    "pending_reboot": False,
}

# Marker die() prefixes the script's own message with on specific, known
# failure kinds — matched below to set error_code. Anything else just
# surfaces as plain (English, untranslated) text via `error`, same as before.
ERROR_CODE_MARKERS = {
    "ERR_ARCH_SERVER_UNREACHABLE": "arch_server_unreachable",
}


class Plugin(StandaloneMixin, PluginUpdaterMixin, WhatsNewSeenMixin, OtherPluginsSeenMixin, CustomBuildMixin):
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
        """Combine production and NFB metadata without changing package sources."""
        return await fetch_catalog(_clean_env(), decky.logger)

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
        return dict(self._state)

    async def get_log_path(self) -> str:
        return str(current_log_path())

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

    async def reboot_system(self) -> bool:
        try:
            await asyncio.create_subprocess_exec("systemctl", "reboot", env=_clean_env())
            return True
        except Exception as e:
            decky.logger.error(f"[reboot_system] {e}")
            return False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _unload(self) -> None:
        # Only detach the observer. systemd continues to own the update job.
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        decky.logger.info("decky-nvidia-update unloaded")

    async def _migration(self) -> None:
        pass
