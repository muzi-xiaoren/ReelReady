"""Import public site metadata from a local PT-Depiler checkout (no user credentials).

Usage: python tools/import_pt_depiler.py PATH_TO_PT_DEPILER
"""

import codecs
import json
from pathlib import Path
import re
import subprocess
import shutil
import sys


def import_catalog(checkout: Path) -> None:
    sites = []
    for path in sorted((checkout / "src/packages/site/definitions").glob("*.ts")):
        source = path.read_text(encoding="utf-8")
        original_source = source
        marker = re.search(r"export const siteMetadata[^=]*=\s*\{", source)
        if not marker:
            continue
        source = source[marker.end():]
        fields = {}
        for field in ("id", "name", "type", "schema"):
            match = re.search(r'^  ' + field + r':\s*"([^"\n]+)"', source, re.M)
            fields[field] = match.group(1) if match else ""
        if not fields["schema"] and re.search(r"export default class \w+ extends NexusPHP\b", original_source):
            fields["schema"] = "NexusPHP"
        if fields["type"] != "private":
            continue
        urls_match = re.search(r'^  urls:\s*\[([^\]]*)\]', source, re.M)
        urls = []
        for url in re.findall(r'"([^"\n]+)"', urls_match.group(1) if urls_match else ""):
            if url.startswith(("uggcf://", "uggc://")):
                url = codecs.decode(url, "rot_13")
            if url.startswith(("https://", "http://")):
                urls.append(url)
        if fields["id"] and urls:
            favicon = re.search(r'^  favicon:\s*"\./([^"/]+)"', source, re.M)
            sites.append({"id": fields["id"], "name": fields["name"] or fields["id"],
                          "schema": fields["schema"] or "custom", "urls": urls,
                          "is_dead": bool(re.search(r'^  isDead:\s*true\b', source, re.M)),
                          "_favicon": favicon.group(1) if favicon else None})
    if not sites:
        raise ValueError("No site definitions found; upstream format may have changed")
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    root = Path(__file__).resolve().parents[1]
    icon_dir = root / "reelready/web/static/site-icons"
    icon_dir.mkdir(parents=True, exist_ok=True)
    for entry in sites:
        explicit_icon = entry.pop("_favicon")
        if explicit_icon:
            icon = checkout / "public/icons/site" / explicit_icon
            if icon.is_file():
                shutil.copyfile(icon, icon_dir / icon.name)
                entry["icon"] = icon.name
                continue
        for extension in ("png", "ico", "svg"):
            icon = checkout / "public/icons/site" / f"{entry['id']}.{extension}"
            if icon.is_file():
                shutil.copyfile(icon, icon_dir / icon.name)
                entry["icon"] = icon.name
                break
    destination = root / "reelready/sites/catalog.json"
    destination.write_text(json.dumps({
        "source": "https://github.com/pt-plugins/PT-depiler", "revision": revision,
        "license": "MIT", "sites": sites,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "reelready/sites/PT_DEPILER_LICENSE.txt").write_text(
        "Public site metadata and bundled site icons are derived from PT-Depiler.\n"
        "Source: https://github.com/pt-plugins/PT-depiler\n\n" +
        (checkout / "LICENSE").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Imported {len(sites)} private site definitions ({sum(s['schema'] == 'NexusPHP' for s in sites)} NexusPHP)")


if __name__ == "__main__":
    import_catalog(Path(sys.argv[1]))
