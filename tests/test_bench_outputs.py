from __future__ import annotations

import base64
from pathlib import Path

from schub.bench.outputs import Clip, OutputCollector

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def test_clip_keeps_head_and_tail() -> None:
    clip = Clip(head_max=5, tail_max=3)
    for chunk in ("abc", "defgh", "ijklmnop"):
        clip.add(chunk)
    assert clip.head == "abcde" and clip.tail == "nop"
    assert clip.dropped == len("fghijklm")
    assert "8 characters left out" in clip.text()


def test_streams_merge_and_errors_are_clean(tmp_path: Path) -> None:
    collector = OutputCollector(tmp_path, "c0001", max_chars=600)
    collector.add("stream", {"name": "stdout", "text": "one\n"})
    collector.add("stream", {"name": "stdout", "text": "two\n"})
    collector.add("stream", {"name": "stderr", "text": "warn\n"})
    collector.add("error", {"ename": "KeyError", "evalue": "'gene'",
                            "traceback": ["\x1b[0;31mKeyError\x1b[0m: 'gene'"]})
    items = collector.snapshot()
    assert [(i.kind, i.name) for i in items] == [("stream", "stdout"), ("stream", "stderr"), ("error", "")]
    assert items[0].text == "one\ntwo\n"
    assert items[2].text == "KeyError: 'gene'" and "\x1b" not in items[2].text
    assert collector.error() == ("KeyError", "'gene'")


def test_images_go_to_files(tmp_path: Path) -> None:
    collector = OutputCollector(tmp_path, "c0002", max_chars=600)
    collector.add("display_data", {"data": {"image/png": PNG, "text/plain": "<Figure>"}})
    collector.add("execute_result", {"data": {"text/plain": "42"}})
    items = collector.snapshot()
    assert items[0].image == "cells/c0002/fig-001.png"
    assert (tmp_path / items[0].image).read_bytes().startswith(b"\x89PNG")
    assert items[1].kind == "result" and items[1].text == "42"


def test_long_output_is_truncated_not_lost_silently(tmp_path: Path) -> None:
    collector = OutputCollector(tmp_path, "c0003", max_chars=600)
    collector.add("stream", {"name": "stdout", "text": "x" * 10_000})
    item = collector.snapshot()[0]
    assert item.truncated == 10_000 - 600
    assert len(item.text) < 700


def test_too_many_outputs_are_counted(tmp_path: Path) -> None:
    collector = OutputCollector(tmp_path, "c0004", max_chars=600)
    for i in range(250):
        collector.add("execute_result", {"data": {"text/plain": str(i)}})
    items = collector.snapshot()
    assert len(items) == 201 and "50 more outputs" in items[-1].text
