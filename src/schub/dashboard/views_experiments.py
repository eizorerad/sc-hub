"""Compare (#experiments): every branch as one row, to filter, sort, group and compare.

The rows travel as JSON inside the page (no request needed on file:// pages); the
page script (script_experiments.py) builds the table with text nodes only.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .collect import Snapshot
from .experiments import experiments_payload
from .html import hint
from .views_runs import ImageUrl


def _finite(value: Any) -> Any:
    """NaN and infinities become null: JSON.parse rejects them and would stop the page."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def embedded_json(payload: object) -> str:
    """Strict JSON that is safe inside <script type="application/json"> (no '</script>' or '<!--')."""
    text = json.dumps(_finite(payload), allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_experiments(snap: Snapshot, views: dict[str, str], image_url: ImageUrl) -> str:
    payload = experiments_payload(snap, views, image_url)
    if not payload["rows"]:
        return ('<p class="empty">No experiments yet. Ask your assistant to create a project and save a branch; '
                "every branch becomes a row here.</p>")
    help_text = ("A row is a branch as it is now. Tick 2-5 rows to compare them side by side; click a name for "
                 "its pipeline. Grey numbers come from an older version (it needs a re-run). Pin, archive, tag "
                 "and sweeps: ask your assistant (the ⋯ menu on a branch's pipeline copies the request).")
    return (
        '<div class="exp">'
        f'<div class="exp-top"><div class="exp-bar" id="exp-bar"></div>{hint(help_text, end=True)}</div>'
        '<div class="exp-compare" id="exp-compare" hidden></div>'
        '<div class="exp-table" id="exp-table"></div>'
        f'<script type="application/json" id="exp-data">{embedded_json(payload)}</script></div>'
    )
