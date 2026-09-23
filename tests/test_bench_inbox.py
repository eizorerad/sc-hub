from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from schub.bench.inbox import Inbox
from schub.bench.models import CellRequest


def request(project: str = "demo", cid: str = "c0001", **extra) -> CellRequest:
    fields = {"project": project, "cid": cid, "code": "x = 1", "why": "set x", "expect": "nothing printed",
              "created": "2026-09-24T10:00:00.000+00:00", **extra}
    return CellRequest(**fields)


def test_requests_need_why_expect_and_code() -> None:
    for field in ("why", "expect", "code"):
        with pytest.raises(ValidationError):
            request(**{field: "   "})


def test_claim_takes_requests_once_in_order(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path / "bench")
    inbox.submit(request(cid="c0001"))
    inbox.submit(request(project="ifn/sub", cid="c0002"))
    assert [r.cid for r in inbox.pending()] == ["c0001", "c0002"]
    assert [r.cid for r in inbox.pending("ifn/sub")] == ["c0002"]
    taken = inbox.claim()
    assert [(c.request.project, c.request.cid) for c in taken] == [("demo", "c0001"), ("ifn/sub", "c0002")]
    assert inbox.claim() == [] and inbox.pending() == []
    assert [c.request.cid for c in inbox.claimed()] == ["c0001", "c0002"]
    inbox.done(taken[0])
    assert [c.request.cid for c in inbox.claimed()] == ["c0002"]


def test_garbage_is_set_aside_with_a_reason(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path / "bench")
    (tmp_path / "bench" / "inbox").mkdir(parents=True)
    (tmp_path / "bench" / "inbox" / "0--demo--c0009.json").write_text('{"project": "demo"}')
    assert inbox.claim() == []
    rejected = tmp_path / "bench" / "rejected"
    assert (rejected / "0--demo--c0009.json").exists()
    assert "not a valid" in (rejected / "0--demo--c0009.reason.json").read_text()


def test_controls_are_delivered_once(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path / "bench")
    assert inbox.control("ifn/sub", "c0003", "interrupt") is True
    assert inbox.control("ifn/sub", "c0003", "interrupt") is False  # already pending
    assert inbox.take_controls() == [("ifn/sub", "c0003", "interrupt")]
    assert inbox.take_controls() == []
    with pytest.raises(ValueError):
        inbox.control("demo", "c0001", "rm -rf")
