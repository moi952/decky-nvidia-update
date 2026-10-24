"""NVIDIA metadata: production and New Feature Branch must be queried separately."""
import asyncio
import html
import json
import re
import urllib.parse


# This undocumented endpoint also backs NVIDIA's driver search. qnf=0 omits
# NFB releases, even when they are already packaged by Arch. Keep both queries;
# neither response alone is a complete release-notes/category catalog.
LOOKUP_URL = (
    "https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/"
    "AjaxDriverService.php?func=DriverManualLookup&psid=131&pfid=1068&osID=12"
    "&languageCode=1033&isWHQL=0&beta=null&dltype=-1&dch=0&upCRD=null"
    "&ctk=null&sort1=0&numberOfResults=200&qnf="
)


def notes_to_lines(raw):
    """Convert NVIDIA's URL-encoded HTML notes to plain text, never rendered HTML."""
    if not isinstance(raw, str) or not raw:
        return []
    text = urllib.parse.unquote(raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<a[^>]*>(.*?)</a>", r"\1", text)
    items = re.findall(r"(?is)<li[^>]*>(.*?)</li>", text) or [text]
    lines = []
    for item in items:
        item = re.sub(r"(?i)<[^>]+>", "", item)
        item = re.sub(r"\s+", " ", html.unescape(item)).strip()
        if item:
            lines.append(item)
    return lines


def merge_catalogs(catalogs):
    """Production wins duplicate categories; a duplicate can fill missing notes."""
    versions = {}
    for entries in catalogs:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            version = entry.get("DisplayVersion")
            if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
                continue
            category = next((name for flag, name in (
                ("IsRecommended", "recommended"), ("IsFeaturePreview", "nfb"), ("IsBeta", "beta")
            ) if str(entry.get(flag)) == "1"), None)
            if category is None:
                continue
            notes = notes_to_lines(entry.get("ReleaseNotes"))
            date = entry.get("ReleaseDateTime")
            date = date if isinstance(date, str) else ""
            previous = versions.get(version)
            if previous is None:
                versions[version] = {"category": category, "date": date, "notes": notes}
            else:
                if not previous["notes"]:
                    previous["notes"] = notes
                if not previous["date"]:
                    previous["date"] = date
    return versions


async def fetch_catalog(env, logger):
    async def fetch_channel(channel):
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sfL", "--connect-timeout", "8", "--max-time", "15",
                LOOKUP_URL + str(channel), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env,
            )
            try:
                out, _ = await proc.communicate()
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"curl exited with {proc.returncode}")
            data = json.loads(out.decode(errors="replace"))
            if not isinstance(data, dict) or not isinstance(data.get("IDS"), list):
                raise ValueError("Invalid NVIDIA catalog response")
            return [e["downloadInfo"] for e in data["IDS"]
                    if isinstance(e, dict) and isinstance(e.get("downloadInfo"), dict)]
        except Exception as error:
            logger.error(f"[get_nvidia_branch_info] qnf={channel}: {error}")
            return []

    # gather preserves query order, so a promoted production entry has priority
    # over its historical NFB entry regardless of which HTTP response arrives first.
    catalogs = await asyncio.gather(fetch_channel(0), fetch_channel(1))
    versions = merge_catalogs(catalogs)
    logger.info(f"[get_nvidia_branch_info] production={len(catalogs[0])}, "
                f"NFB={len(catalogs[1])}, classified={len(versions)}")
    return versions
