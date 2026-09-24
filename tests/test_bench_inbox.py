from __future__ import annotations

import os
import time
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
    taken = inbox.claim("900")
    assert [(c.request.project, c.request.cid, c.owner) for c in taken] == [
        ("demo", "c0001", "900"), ("ifn/sub", "c0002", "900")]
    assert inbox.claim("901") == [] and inbox.pending() == []
    assert [c.request.cid for c in inbox.claimed()] == ["c0001", "c0002"]
    assert inbox.claimed("901") == [] and inbox.owners() == ["900", "901"]
    inbox.done(taken[0])
    assert [c.request.cid for c in inbox.claimed("900")] == ["c0002"]
    inbox.requeue(taken[1])
    assert [r.cid for r in inbox.pending()] == ["c0002"]


def test_garbage_is_set_aside_with_a_reason(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path / "bench")
    (tmp_path / "bench" / "inbox").mkdir(parents=True)
    garbage = tmp_path / "bench" / "inbox" / "0--demo--c0009.json"
    garbage.write_text('{"project": "demo"}')
    assert inbox.claim("900") == [] and garbage.exists()  # maybe still being written: left for a few seconds
    os.utime(garbage, (time.time() - 60, time.time() - 60))
    assert inbox.claim("900") == []
    rejected = tmp_path / "bench" / "rejected"
    assert (rejected / "0--demo--c0009.json").exists()
    assert "not a valid" in (rejected / "0--demo--c0009.reason.json").read_text()


def test_controls_are_delivered_once(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path / "bench")
    assert inbox.control("ifn/sub", "c0003", "interrupt") is True
    assert inbox.control("ifn/sub", "c0003", "interrupt") is False  # already pending
    assert inbox.take_controls() == [("ifn/sub", "c0003", "interrupt")]
    assert inbox.take_controls() == []
    assert inbox.control("2024--pbmc", "c0001", "interrupt")  # a project name that starts like a timestamp
    assert inbox.take_controls() == [("2024--pbmc", "c0001", "interrupt")]
    with pytest.raises(ValueError):
        inbox.control("demo", "c0001", "rm -rf")
    with pytest.raises(ValueError):
        inbox.control("demo", "../../etc", "interrupt")
    with pytest.raises(ValueError):
        inbox.claim("../x")


def test_project_names_with_double_dashes(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path / "bench")
    inbox.submit(request(project="a--b", cid="c0001"))
    inbox.submit(request(project="b", cid="c0001"))
    assert [r.project for r in inbox.pending("a")] == []
    assert [r.project for r in inbox.pending("b")] == ["b"]
    inbox.control("a--b", "c0001", "interrupt")
    assert inbox.take_controls() == [("a--b", "c0001", "interrupt")]
    claimed = {c.request.project: c for c in inbox.claim("7")}
    inbox.reject(claimed["a--b"], "for a--b")
    assert inbox.rejected_reason("b", "c0001") is None
    assert inbox.rejected_reason("a--b", "c0001") == "for a--b"


def test_bad_ids_never_become_paths() -> None:
    for bad in ({"cid": "/abs/escaped"}, {"cid": "c1"}, {"project": "../up"}, {"project": "A"}):
        with pytest.raises(ValidationError):
            request(**bad)


def test_a_read_error_leaves_the_request_waiting(tmp_path: Path, monkeypatch) -> None:
    """ESTALE/EIO while reading a claim is the file server's hiccup, not an invalid request."""
    inbox = Inbox(tmp_path / "bench")
    inbox.submit(CellRequest(project="demo", cid="c0001", code="1", why="w", expect="e", created="2026-09-24T00:00:00Z"))
    real = Path.read_bytes

    def flaky(self):
        if self.parent.name == "1":
            raise OSError(116, "Stale file handle")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    assert inbox.claim("1") == [] and inbox.pending()  # back in the inbox, not rejected
    monkeypatch.setattr(Path, "read_bytes", real)
    assert [c.request.cid for c in inbox.claim("1")] == ["c0001"]

