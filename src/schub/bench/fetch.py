"""`bench.fetch(url)` inside a cell: a resumable download that the journal records.

It resumes an interrupted download from the bytes already on disk, re-reading the
size before every attempt (curl's own --retry once corrupted files this way in
VCC2026), checks the HTTP status and the length, hashes the result and records
url, path, size and sha256 in the cell's journal entry. An expected sha256 is
checked; a mismatch deletes the file. A size check alone once passed a 404 error
page, so the status is always checked too.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import ledger
from .fsio import read_json, write_json_atomic

CHUNK = 1 << 20
ATTEMPTS = 5
TIMEOUT_S = 60
SCHEMES = ("https", "http")  # GEO and the others serve https (urllib's ftp has no status to check)
USER_AGENT = "sc-hub-bench/1 (+https://github.com/eizorerad/sc-hub)"


class FetchError(RuntimeError):
    pass


def default_dir() -> Path:
    project = os.environ.get("SCHUB_PROJECT_DIR")
    return Path(project) / "data" if project else Path.cwd() / "data"


def _target(url: str, dest: str | os.PathLike | None) -> Path:
    name = Path(urlsplit(url).path).name or "download"
    if dest is None:
        return default_dir() / name
    path = Path(dest)
    return path / name if path.is_dir() or str(dest).endswith("/") else path


def _open(url: str, offset: int, validator: str = ""):
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
        if validator:
            headers["If-Range"] = validator  # the server sends the whole file if it changed
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_S)


def _sidecar(part: Path) -> Path:
    return part.with_name(part.name + ".json")


def _resumable(url: str, part: Path) -> tuple[int, str]:
    """Bytes to resume from and the validator, only if the partial file is from this URL."""
    meta = read_json(_sidecar(part))
    if not part.exists() or meta is None or meta.get("url") != url:
        part.unlink(missing_ok=True)
        _sidecar(part).unlink(missing_ok=True)
        return 0, ""
    return part.stat().st_size, str(meta.get("validator", ""))


def _expected_total(response, offset: int) -> int | None:
    status = getattr(response, "status", 200)
    if status == 206:
        match = re.search(r"/(\d+)$", response.headers.get("Content-Range", ""))
        return int(match.group(1)) if match else None
    length = response.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def _complete_416(exc: urllib.error.HTTPError, offset: int) -> bool:
    """416 means "nothing after this offset": fine only if the file is exactly that long."""
    match = re.search(r"\*/(\d+)$", exc.headers.get("Content-Range", "") if exc.headers else "")
    return bool(match) and int(match.group(1)) == offset


def _attempt(url: str, part: Path) -> int | None:
    """Download into `part` from where it stops. Returns the expected total, if known."""
    offset, validator = _resumable(url, part)
    try:
        response = _open(url, offset, validator)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset and _complete_416(exc, offset):
            return offset
        raise FetchError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    with response:
        status = getattr(response, "status", None)
        if status not in (200, 206):
            raise FetchError(f"{url}: HTTP {status}")
        if status == 206 and not re.match(rf"bytes {offset}-", response.headers.get("Content-Range", "")):
            raise FetchError(f"{url}: the server resumed from another byte than {offset}")
        total = _expected_total(response, offset)
        write_json_atomic(_sidecar(part), {"url": url, "total": total, "validator":
                                           response.headers.get("ETag") or response.headers.get("Last-Modified") or ""})
        mode = "ab" if status == 206 and offset else "wb"  # a 200 means the server restarted from 0
        with part.open(mode) as handle:
            while block := response.read(CHUNK):
                handle.write(block)
    if total is not None and part.stat().st_size < total:
        # read() just returns less when the server drops the connection: resume, do not accept it
        raise ConnectionError(f"connection closed at {part.stat().st_size} of {total} bytes")
    return total


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, dest: str | os.PathLike | None = None, sha256: str | None = None,
          attempts: int = ATTEMPTS, pause_s: float = 5.0) -> Path:
    """Download `url` (default: the project's data/ folder) and record it in the journal."""
    if urlsplit(url).scheme not in SCHEMES:
        raise FetchError(f"only {', '.join(SCHEMES)} URLs can be fetched")
    target = _target(url, dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    total = _download(url, part, attempts, pause_s)
    size = part.stat().st_size
    if size == 0 or (total is not None and size != total):
        _discard(part)
        _fail(url, target, f"got {size} bytes, expected {total}")
    digest = sha256_of(part)
    if sha256 and digest != sha256.lower():
        _discard(part)
        _fail(url, target, f"sha256 {digest} does not match the expected {sha256}")
    part.replace(target)
    _sidecar(part).unlink(missing_ok=True)
    ledger.record("download", url=url, path=str(target), size=size, sha256=digest, status="ok")
    print(f"fetched {url}\n  -> {target} ({size / 1e6:.1f} MB, sha256 {digest[:12]}...)")
    return target


def _download(url: str, part: Path, attempts: int, pause_s: float) -> int | None:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _attempt(url, part)
        except FetchError:
            raise  # an HTTP error status: retrying will not help
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
            time.sleep(pause_s * attempt)
    raise FetchError(f"{url}: download failed after {attempts} attempts: {last}")


def _discard(part: Path) -> None:
    part.unlink(missing_ok=True)
    _sidecar(part).unlink(missing_ok=True)


def _fail(url: str, target: Path, message: str) -> None:
    ledger.record("download", url=url, path=str(target), size=0, sha256="", status="failed", message=message)
    raise FetchError(f"{url}: {message}")
