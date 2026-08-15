import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

import asyncio

import decky

# Checks GitHub Releases for a newer decky-nvidia-update build than the one
# currently installed, and surfaces enough info (asset zip URL + checksum)
# for the frontend to hand off to Decky Loader's own installer
# (utilities/install_plugin) — the same mechanism the Decky plugin store
# itself uses. No download/extraction happens on the Python side.
GITHUB_REPO = "moi952/decky-nvidia-update"
GITHUB_LATEST_RELEASE_URL = (
    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
)
FALLBACK_RELEASE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"

PLUGIN_JSON_PATH = Path(decky.DECKY_PLUGIN_DIR) / "plugin.json"

# Avoid hammering GitHub's API on every panel open — a manual "check now"
# (force=True) bypasses this.
CACHE_TTL_SECONDS = 3600


def _clean_env() -> Dict[str, str]:
    """Same LD_LIBRARY_PATH fix as plugin.py's own helper — duplicated here
    (rather than imported) to avoid a circular import between the two
    modules, since plugin.py imports this file's mixin class."""
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    return env


def _version_tuple(v: str):
    """'1.2.10' -> (1, 2, 10), tolerant of a leading 'v' and non-numeric
    trailing junk (e.g. a '-beta' suffix on a hand-made tag)."""
    parts = []
    for p in v.lstrip("vV").split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


class PluginUpdaterMixin:
    """Checks GitHub Releases for a newer decky-nvidia-update build than the
    one currently installed. Mixed into Plugin (see plugin.py) so its methods
    are callable from the frontend the same way as any other Plugin method."""

    _plugin_update_cache: Dict[str, Any] = {"checked_at": 0.0, "result": None}

    def _read_plugin_json(self) -> Dict[str, Any]:
        try:
            return json.loads(PLUGIN_JSON_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            decky.logger.error(f"[plugin_updater] reading plugin.json: {e}")
            return {}

    async def _fetch_latest_release(self) -> Optional[Dict[str, str]]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sfL",
                "-H", "Accept: application/vnd.github+json",
                "-H", "User-Agent: decky-nvidia-update",
                GITHUB_LATEST_RELEASE_URL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_clean_env(),
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return None
            data = json.loads(out.decode(errors="replace"))
            tag = data.get("tag_name", "")
            if not tag:
                return None

            # release.yml uploads exactly one asset per release (the zipped
            # plugin build) — pick whichever asset actually looks like it.
            assets = data.get("assets", []) or []
            zip_asset = next(
                (a for a in assets if str(a.get("name", "")).endswith(".zip")),
                None,
            )
            # GitHub's release-asset "digest" field (sha256:<hex>) isn't
            # guaranteed present on every asset — Decky's own installer
            # treats an empty checksum as "skip verification", so this
            # degrades gracefully either way.
            digest = str((zip_asset or {}).get("digest", "") or "")
            sha256 = digest[len("sha256:"):] if digest.startswith("sha256:") else ""

            return {
                "tag": tag,
                "url": data.get("html_url", FALLBACK_RELEASE_URL),
                "asset_url": (zip_asset or {}).get("browser_download_url", ""),
                "sha256": sha256,
            }
        except Exception as e:
            decky.logger.error(f"[plugin_updater] fetch failed: {e}")
            return None

    async def get_plugin_update_info(self, force: bool = False) -> Dict[str, Any]:
        cache = PluginUpdaterMixin._plugin_update_cache
        now = time.monotonic()
        if (
            not force
            and cache["result"] is not None
            and (now - cache["checked_at"]) < CACHE_TTL_SECONDS
        ):
            return cache["result"]

        plugin_json = self._read_plugin_json()
        current = str(plugin_json.get("version", ""))
        # Passed through to Decky's own installer call and back to us via
        # its loader progress events — needs to be whatever Decky's install
        # flow itself uses to label/track this install, so it comes straight
        # from plugin.json rather than being hardcoded twice.
        display_name = str(plugin_json.get("name", "Decky NVIDIA Update"))

        release = await self._fetch_latest_release()
        if release is None:
            result = {
                "current_version": current,
                "latest_version": "",
                "has_update": False,
                "release_url": FALLBACK_RELEASE_URL,
                "asset_url": "",
                "sha256": "",
                "plugin_display_name": display_name,
                "checked_ok": False,
            }
        else:
            latest = release["tag"].lstrip("vV")
            result = {
                "current_version": current,
                "latest_version": latest,
                "has_update": bool(latest)
                and _version_tuple(latest) > _version_tuple(current),
                "release_url": release["url"],
                "asset_url": release.get("asset_url", ""),
                "sha256": release.get("sha256", ""),
                "plugin_display_name": display_name,
                "checked_ok": True,
            }
        cache["result"] = result
        cache["checked_at"] = now
        return result
