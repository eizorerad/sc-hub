"""Events recorded inside the kernel (downloads, submitted jobs) until the runner
collects them after the cell, through the execute request's user_expressions.

Helpers that run in the kernel (fetch, %%slurm) call `record`; the runner asks
for DRAIN_EXPRESSION and gets the events of that cell as JSON.
"""

from __future__ import annotations

import ast
import json
import threading
from typing import Any

KINDS = ("download", "job", "note")
DRAIN_EXPRESSION = "__import__('schub.bench.ledger', fromlist=['drain_json']).drain_json()"
_events: list[dict[str, Any]] = []
_lock = threading.Lock()


def record(kind: str, **payload: Any) -> None:
    if kind not in KINDS:
        raise ValueError(f"ledger kind must be one of {KINDS}")
    with _lock:
        _events.append({"kind": kind, **payload})


def drain() -> list[dict[str, Any]]:
    with _lock:
        taken = list(_events)
        _events.clear()
    return taken


def drain_json() -> str:
    return json.dumps(drain(), default=str)


def parse_user_expression(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Events from the user_expressions reply (text/plain is the repr of the JSON string)."""
    if not result or result.get("status") != "ok":
        return []
    text = (result.get("data") or {}).get("text/plain", "")
    try:
        events = json.loads(ast.literal_eval(text))
    except (ValueError, SyntaxError, TypeError):
        return []
    return [e for e in events if isinstance(e, dict) and e.get("kind") in KINDS] if isinstance(events, list) else []
