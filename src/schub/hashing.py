"""Stable content hashes for plans, steps and dataset fingerprints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def stable_hash(*parts: Any, length: int = 16) -> str:
    payload = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:length]


def file_fingerprint(path: Path) -> str:
    """Identity of a file version: resolved path, size and modification time."""
    stat = path.stat()
    return stable_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
