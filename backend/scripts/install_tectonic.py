"""Fetch the Tectonic binary this project compiles résumés with.

    python -m scripts.install_tectonic

Downloads the release build for this platform into `backend/.tools/`, where
`app/latex.py` looks for it. The binary is ~50MB and gitignored — it is a tool,
not source.

Why not a package manager: there is no TeX distribution on this machine and
installing one is ~500MB that then has to be repeated in the Docker image.
Tectonic is one file that fetches only the support files a document actually
needs, and caches them. The first compile after this is slow (~5 min, building
that cache); every one after is ~3.5s.
"""

from __future__ import annotations

import argparse
import io
import platform
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BACKEND_DIR / ".tools"
RELEASES = "https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest"

# Substrings identifying the right asset, per platform. Tectonic publishes
# several builds per OS; these pick the one with no extra runtime dependency.
ASSET_HINTS: dict[tuple[str, str], str] = {
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("Linux", "aarch64"): "aarch64-unknown-linux-musl",
}


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ai-job-resume-builder"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed host
        return response.read()


def _pick_asset(payload: dict, hint: str) -> tuple[str, str]:
    for asset in payload.get("assets", []):
        name = asset.get("name", "")
        if hint in name and name.endswith((".zip", ".tar.gz")):
            return name, asset["browser_download_url"]
    raise SystemExit(f"no Tectonic asset matching {hint!r} in {payload.get('tag_name')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()

    name = "tectonic.exe" if platform.system() == "Windows" else "tectonic"
    target = TOOLS_DIR / name
    if target.is_file() and not args.force:
        print(f"already installed: {target}")
        return 0

    key = (platform.system(), platform.machine())
    hint = ASSET_HINTS.get(key)
    if hint is None:
        raise SystemExit(
            f"no known Tectonic build for {key}. Install it yourself and set TECTONIC_PATH."
        )

    import json

    print("resolving latest release ...")
    payload = json.loads(_fetch(RELEASES))
    asset_name, url = _pick_asset(payload, hint)
    print(f"downloading {asset_name} ...")
    blob = _fetch(url)

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            member = next(m for m in archive.namelist() if m.endswith(name))
            target.write_bytes(archive.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            member = next(m for m in archive.getnames() if m.endswith(name))
            extracted = archive.extractfile(member)
            assert extracted is not None
            target.write_bytes(extracted.read())

    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed: {target}")
    print("the first compile builds the support-file cache and takes a few minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
