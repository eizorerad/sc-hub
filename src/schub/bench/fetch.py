"""`bench.fetch(url)` inside a cell: a resumable download that the journal records.

It resumes an interrupted download from the bytes already on disk, re-reading the
size before every attempt (curl's own --retry once corrupted files this way in
VCC2026), checks the HTTP status and the length, hashes the result and records
url, path, size and sha256 in the cell's journal entry. An expected sha256 is
checked; a mismatch deletes the file. A size check alone once passed a 404 error
page, so the status is always checked too.
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import ledger
from .fsio import read_json, write_json_atomic

CHUNK = 1 << 20
ATTEMPTS = 5
TIMEOUT_S = 60
SCHEMES = ("https", "http")  # GEO and the others serve https (urllib's ftp has no status to check)
USER_AGENT = "sc-hub-bench/1 (+https://github.com/eizorerad/sc-hub)"
SECRET_PARAM = re.compile(r"token|key|sig|signature|secret|password|passwd|auth|credential|session|^x-amz-", re.I)


def public(url: str) -> str:
    """The URL as the journal shows it: values of secret-looking query parameters are hidden."""
    parts = urlsplit(url)
    query = [(k, "REDACTED" if SECRET_PARAM.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(query, safe="/:")))


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


def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _resumable(url: str, part: Path) -> tuple[int, str]:
    """Bytes to resume from and the validator, only if the partial file is from this URL."""
    meta = read_json(_sidecar(part))
    if not part.exists() or meta is None or meta.get("url_sha256") != _url_key(url):
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
        write_json_atomic(_sidecar(part), {"url": public(url), "url_sha256": _url_key(url), "total": total, "validator":
                                           response.headers.get("ETag") or response.headers.get("Last-Modified") or ""})
        mode = "ab" if status == 206 and offset else "wb"  # a 200 means the server restarted from 0
        with part.open(mode) as handle:
            while block := response.read(CHUNK):
                handle.write(block)
    if total is not None and part.stat().st_size < total:
        # read() just returns less when the server drops the connection: resume, do not accept it
        raise ConnectionError(f"connection closed at {part.stat().st_size} of {total} bytes")
    return total


def digests_of(path: Path) -> tuple[str, str]:
    """(sha256, md5) in one read; Zenodo publishes md5, the journal records sha256."""
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def sha256_of(path: Path) -> str:
    return digests_of(path)[0]


def fetch(url: str, dest: str | os.PathLike | None = None, sha256: str | None = None, md5: str | None = None,
          attempts: int = ATTEMPTS, pause_s: float = 5.0) -> Path:
    """Download `url` (default: the project's data/ folder) and record it in the journal.
    `sha256` / `md5`: the checksum the source publishes (Zenodo gives md5); a mismatch deletes the file."""
    parts = urlsplit(url)
    if parts.scheme not in SCHEMES:
        raise FetchError(f"only {', '.join(SCHEMES)} URLs can be fetched")
    if parts.username or parts.password:
        raise FetchError("a URL with a user or password would put it in the journal; use a public link")
    target = _target(url, dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    cache = _cache_path(target.name, sha256, md5)
    if cache is not None:
        return _via_cache(url, target, cache, sha256, md5, attempts, pause_s)
    with open(target.with_name(target.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # jobs fetching the same file take turns; the second finds it done
        return _fetch_locked(url, target, sha256, md5, attempts, pause_s)


def _cache_path(name: str, sha256: str | None, md5: str | None) -> Path | None:
    """With a published checksum, one copy per student: $SCHUB_ROOT/cache/fetch/<checksum>/<name>."""
    root = os.environ.get("SCHUB_ROOT")
    if not root or not (sha256 or md5):
        return None
    key = f"sha256-{sha256.lower()}" if sha256 else f"md5-{md5.lower().removeprefix('md5:')}"
    if not re.fullmatch(r"(sha256-[0-9a-f]{64}|md5-[0-9a-f]{32})", key):
        raise FetchError(f"{key.split('-')[0]}: not a hexadecimal checksum")
    return Path(root) / "cache" / "fetch" / key / name


def _via_cache(url: str, target: Path, cache: Path, sha256: str | None, md5: str | None, attempts: int,
               pause_s: float) -> Path:
    """Projects asking for the same checksummed file share one download (the first fetches, the others wait
    on the lock and link it); every project's journal still records its own copy."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    record_path = cache.parent / "record.json"
    with open(cache.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = read_json(record_path) or {}
        fresh = not (cache.exists() and record.get("size") == cache.stat().st_size)
        if fresh:
            _fetch_locked(url, cache, sha256, md5, attempts, pause_s, record=False)
            digest = digests_of(cache)[0]
            write_json_atomic(record_path, {"url": public(url), "size": cache.stat().st_size, "sha256": digest})
            record = read_json(record_path) or {}
        _link(cache, target)
    size, digest = int(record["size"]), str(record["sha256"])
    note = "" if fresh else "from the student's download cache (the same checksum was fetched before)"
    ledger.record("download", url=public(url), path=str(target), size=size, sha256=digest, status="ok",
                  message=note)
    print(f"fetched {public(url)}\n  -> {target} ({size / 1e6:.1f} MB, sha256 {digest[:12]}...)"
          + ("\n  (from the download cache)" if note else ""))
    return target


def _link(cache: Path, target: Path) -> None:
    if target.exists() and target.stat().st_ino == cache.stat().st_ino:
        return
    temp = target.with_name(f".{target.name}.link")
    temp.unlink(missing_ok=True)
    try:
        os.link(cache, temp)  # same file system: no second copy
    except OSError:
        shutil.copy2(cache, temp)
    temp.replace(target)


def _fetch_locked(url: str, target: Path, sha256: str | None, md5: str | None, attempts: int,
                  pause_s: float, record: bool = True) -> Path:
    shown = public(url)
    if target.exists() and (sha256 or md5):
        digest, md5_digest = digests_of(target)
        if (not sha256 or digest == sha256.lower()) and (not md5 or md5_digest == md5.lower().removeprefix("md5:")):
            ledger.record("download", url=shown, path=str(target), size=target.stat().st_size, sha256=digest,
                          status="ok", message="already there with the expected checksum")
            print(f"{target} is already there with the expected checksum")
            return target
    part = target.with_name(target.name + ".part")
    total = _download(url, part, attempts, pause_s)
    size = part.stat().st_size
    if size == 0 or (total is not None and size != total):
        _discard(part)
        _fail(shown, target, f"got {size} bytes, expected {total}")
    digest, md5_digest = digests_of(part)
    if sha256 and digest != sha256.lower():
        _discard(part)
        _fail(shown, target, f"sha256 {digest} does not match the expected {sha256}")
    if md5 and md5_digest != md5.lower().removeprefix("md5:"):
        _discard(part)
        _fail(shown, target, f"md5 {md5_digest} does not match the expected {md5}")
    part.replace(target)
    _sidecar(part).unlink(missing_ok=True)
    unverified = total is None and not (sha256 or md5)
    note = "the server sent no length and no checksum was given: the size is not verified" if unverified else ""
    if record:
        ledger.record("download", url=shown, path=str(target), size=size, sha256=digest, status="ok", message=note)
        print(f"fetched {shown}\n  -> {target} ({size / 1e6:.1f} MB, sha256 {digest[:12]}...)"
              + (f"\n  warning: {note}" if note else ""))
    return target


def _download(url: str, part: Path, attempts: int, pause_s: float) -> int | None:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _attempt(url, part)
        except FetchError:
            raise  # an HTTP error status: retrying will not help
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as exc:
            last = exc
            time.sleep(pause_s * attempt)
    raise FetchError(f"{url}: download failed after {attempts} attempts: {last}")


def _discard(part: Path) -> None:
    part.unlink(missing_ok=True)
    _sidecar(part).unlink(missing_ok=True)


def _fail(url: str, target: Path, message: str) -> None:
    ledger.record("download", url=url, path=str(target), size=0, sha256="", status="failed", message=message)
    raise FetchError(f"{url}: {message}")
