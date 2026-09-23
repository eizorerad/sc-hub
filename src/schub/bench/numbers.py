"""Do the numbers in a finding come from the cells it cites?

Every number of 10 or more in the text (years and small counts aside) is looked up
in the outputs and check messages of the cited cells, allowing for rounding (0.5%)
and for percentages written as fractions. The ones not found are recorded with the
note and shown next to it: a number nobody can trace is either from elsewhere (say
so) or a mistake. In VCC2026 the submission court accepted a ballot only if each of
its numbers was found in the cited file.
"""

from __future__ import annotations

import re
from typing import Iterable

NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?%?")
TOLERANCE = 0.005


def _value(token: str) -> float | None:
    try:
        return float(token.rstrip("%").replace(",", ""))
    except ValueError:
        return None


def claimed_numbers(text: str) -> list[str]:
    found = []
    for token in NUMBER.findall(text):
        value = _value(token)
        if value is None:
            continue
        is_int = "." not in token and not token.endswith("%")
        if is_int and (abs(value) < 10 or (1900 <= value <= 2100 and len(token.replace(",", "")) == 4)):
            continue  # small counts and years are not measurements to trace
        found.append(token)
    return found


def _decimals(token: str) -> int:
    digits = token.rstrip("%").partition(".")[2]
    return len(digits)


def _close(claimed: float, measured: float, decimals: int) -> bool:
    """Equal after rounding to the claimed precision, or within 0.5% of each other."""
    if abs(claimed - measured) <= 0.5 * 10 ** -decimals + 1e-12:
        return True
    return abs(claimed - measured) <= TOLERANCE * max(abs(claimed), abs(measured))


def unresolved(text: str, evidence: Iterable[str]) -> tuple[str, ...]:
    measured = [v for chunk in evidence for v in (_value(t) for t in NUMBER.findall(chunk)) if v is not None]
    missing = []
    for token in claimed_numbers(text):
        value, decimals = _value(token), _decimals(token)
        # 87% may be printed as 87.1 or as 0.871; 0.87 may be printed as 87.
        pairs = [(value, decimals), (value / 100, decimals + 2)] if token.endswith("%") else \
            [(value, decimals), (value * 100, max(0, decimals - 2))]
        if not any(_close(c, m, d) for c, d in pairs for m in measured):
            missing.append(token)
    return tuple(dict.fromkeys(missing))
