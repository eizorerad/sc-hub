from __future__ import annotations

import pytest

from schub.bench import ledger


def test_events_are_drained_once() -> None:
    ledger.drain()
    ledger.record("download", url="https://x/y", path="data/y", size=3, sha256="ab")
    ledger.record("job", job_id="812")
    text = ledger.drain_json()
    assert ledger.drain() == []
    reply = {"status": "ok", "data": {"text/plain": repr(text)}}
    assert [e["kind"] for e in ledger.parse_user_expression(reply)] == ["download", "job"]


def test_unknown_kinds_and_bad_replies() -> None:
    with pytest.raises(ValueError):
        ledger.record("rm", path="/")
    assert ledger.parse_user_expression(None) == []
    assert ledger.parse_user_expression({"status": "error"}) == []
    assert ledger.parse_user_expression({"status": "ok", "data": {"text/plain": "not a repr"}}) == []
    bogus = repr('[{"kind": "evil"}, 3]')
    assert ledger.parse_user_expression({"status": "ok", "data": {"text/plain": bogus}}) == []
