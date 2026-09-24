"""Kernel output messages -> journal output items.

Long text keeps its head and tail (the middle is counted, not kept). Images are
saved next to the entry and referenced by path, so the journal stays small and
the dashboard can show them.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import OutputItem

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MAX_IMAGES = 50
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ITEMS = 200


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


@dataclass
class Clip:
    """Text that keeps at most `head` characters from the start and `tail` from the end."""

    head_max: int
    tail_max: int
    head: str = ""
    tail: str = ""
    dropped: int = 0

    def add(self, text: str) -> None:
        if len(self.head) < self.head_max:
            room = self.head_max - len(self.head)
            self.head += text[:room]
            text = text[room:]
        if not text:
            return
        combined = self.tail + text
        overflow = len(combined) - self.tail_max
        if overflow > 0:
            self.dropped += overflow
            combined = combined[overflow:]
        self.tail = combined

    def text(self) -> str:
        if not self.dropped:
            return self.head + self.tail
        return f"{self.head}\n[... {self.dropped} characters left out ...]\n{self.tail}"


@dataclass
class _Item:
    kind: str
    name: str = ""
    clip: Clip | None = None
    image: str = ""
    ename: str = ""
    evalue: str = ""


@dataclass
class OutputCollector:
    """Collects the outputs of one cell. `journal_dir` is where image paths are relative to."""

    journal_dir: Path
    cid: str
    max_chars: int
    items: list[_Item] = field(default_factory=list)
    images: int = 0
    dropped_items: int = 0

    def _clip(self) -> Clip:
        return Clip(head_max=self.max_chars * 2 // 3, tail_max=self.max_chars // 3)

    def _append(self, item: _Item) -> None:
        if len(self.items) >= MAX_ITEMS:
            if item.kind == "error":  # the traceback matters more than the 200th stream chunk before it
                victim = next((i for i, old in enumerate(self.items) if old.kind != "error"), None)
                if victim is not None:
                    del self.items[victim]
                    self.dropped_items += 1
                    self.items.append(item)
                    return
            self.dropped_items += 1
            return
        self.items.append(item)

    def add(self, msg_type: str, content: dict[str, Any]) -> None:
        if msg_type == "stream":
            self._stream(str(content.get("name", "stdout")), str(content.get("text", "")))
        elif msg_type in ("execute_result", "display_data", "update_display_data"):
            self._rich("result" if msg_type == "execute_result" else "display", content.get("data") or {})
        elif msg_type == "error":
            clip = self._clip()
            clip.add(strip_ansi("\n".join(content.get("traceback") or [])))
            self._append(_Item(kind="error", clip=clip, ename=str(content.get("ename", "")),
                               evalue=strip_ansi(str(content.get("evalue", "")))[:2000]))

    def add_text(self, name: str, text: str) -> None:
        """Text from the runner itself (e.g. why a cell was stopped)."""
        self._stream(name, text)

    def _stream(self, name: str, text: str) -> None:
        last = self.items[-1] if self.items else None
        if last is not None and last.kind == "stream" and last.name == name and last.clip is not None:
            last.clip.add(strip_ansi(text))
            return
        clip = self._clip()
        clip.add(strip_ansi(text))
        self._append(_Item(kind="stream", name=name, clip=clip))

    def _rich(self, kind: str, data: dict[str, Any]) -> None:
        image = self._save_image(data.get("image/png")) if "image/png" in data else ""
        text = data.get("text/plain", "")
        if isinstance(text, list):
            text = "".join(text)
        clip = self._clip()
        clip.add(strip_ansi(str(text)) if not image else "")
        self._append(_Item(kind=kind, clip=clip, image=image))

    def _save_image(self, encoded: Any) -> str:
        if self.images >= MAX_IMAGES or not isinstance(encoded, str):
            return ""
        try:
            data = base64.b64decode(encoded, validate=False)
        except (binascii.Error, ValueError):
            return ""
        if not data or len(data) > MAX_IMAGE_BYTES:
            return ""
        self.images += 1
        folder = self.journal_dir / "cells" / self.cid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"fig-{self.images:03d}.png"
        path.write_bytes(data)
        return path.relative_to(self.journal_dir).as_posix()

    def snapshot(self) -> tuple[OutputItem, ...]:
        found = [
            OutputItem(kind=i.kind, name=i.name, text=i.clip.text() if i.clip else "", image=i.image,
                       ename=i.ename, evalue=i.evalue, truncated=i.clip.dropped if i.clip else 0)
            for i in self.items
        ]
        if self.dropped_items:
            found.append(OutputItem(kind="stream", name="stderr",
                                    text=f"[{self.dropped_items} more outputs left out]"))
        return tuple(found)

    def error(self) -> tuple[str, str] | None:
        last = next((i for i in reversed(self.items) if i.kind == "error"), None)
        return (last.ename, last.evalue) if last else None
