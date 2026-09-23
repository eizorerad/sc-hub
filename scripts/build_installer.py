"""Build the self-contained installers in dist/ from installer/*.in.

The sc-hub source (pyproject, README, src, scripts, templates) is packed into a
reproducible tar.gz, base64-encoded and embedded, so one downloaded file is all
a student needs, even without access to the shared library.

    python scripts/build_installer.py
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ("pyproject.toml", "README.md", "src", "scripts", "templates")
EXCLUDE_PARTS = {"__pycache__", ".DS_Store", ".pytest_cache"}
TEMPLATES = {"install.sh.in": "install-sc-hub.sh", "install.ps1.in": "install-sc-hub.ps1"}


def _files() -> list[Path]:
    found = []
    for name in INCLUDE:
        path = ROOT / name
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        found += [p for p in candidates if not (EXCLUDE_PARTS & set(p.parts)) and not p.name.endswith(".egg-info")]
    return found


def build_bundle() -> bytes:
    """A byte-identical archive for identical sources (fixed order, times and owners)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in _files():
            info = tar.gettarinfo(str(path), arcname=str(path.relative_to(ROOT)))
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            info.mode = 0o755 if path.stat().st_mode & 0o100 else 0o644
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    return gzip.compress(raw.getvalue(), mtime=0)


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    if match is None:
        raise SystemExit("version not found in pyproject.toml")
    return match.group(1)


def main() -> int:
    bundle = build_bundle()
    tag = f"{version()}+{hashlib.sha256(bundle).hexdigest()[:8]}"
    encoded = "\n".join(textwrap.wrap(base64.b64encode(bundle).decode(), 76))
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    for template, output in TEMPLATES.items():
        text = (ROOT / "installer" / template).read_text()
        target = dist / output
        target.write_text(text.replace("__VERSION__", tag).replace("__BUNDLE__", encoded))
        target.chmod(0o755)
    check = subprocess.run(["bash", "-n", str(dist / "install-sc-hub.sh")], capture_output=True, text=True)
    if check.returncode != 0:
        sys.stderr.write(check.stderr)
        return 1
    sys.stdout.write(f"built {tag}: {len(bundle) // 1024} KB bundle, installers in {dist}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
