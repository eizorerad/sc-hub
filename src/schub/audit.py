"""Append-only log of every tool call: the data source for pilot metrics."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MAX_VALUE_CHARS = 300


def _compact(value: Any) -> Any:
    text = json.dumps(value, default=str)
    return value if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."


@contextmanager
def audited(log_dir: Path, tool: str, args: dict[str, Any]) -> Iterator[None]:
    started = time.monotonic()
    outcome: dict[str, Any] = {"ok": True}
    try:
        yield
    except Exception as exc:
        outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:MAX_VALUE_CHARS]}
        raise
    finally:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "args": {k: _compact(v) for k, v in args.items()},
            "duration_s": round(time.monotonic() - started, 3),
            **outcome,
        }
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            with (log_dir / f"calls-{month}.jsonl").open("a") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass  # auditing must never break the tool call itself
