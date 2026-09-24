"""What an author asks for when writing a report, and what the tool answers.

The author (the lab agent's writer turn, or a chat assistant) writes the prose and
picks what to show; sc-hub assembles the notebook, taking code, outputs, figures and
notes verbatim from the journal (report_build.py), so nothing in it was invented.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..state import Frozen

KINDS = ("text", "cell", "figure", "note")
Show = Literal["all", "outputs", "code"]


class Block(Frozen):
    """One piece of the report: exactly one of text, cell, figure or note."""

    text: str = Field(default="", max_length=20_000)  # markdown written by the author
    cell: str = Field(default="", max_length=120)  # "c0012": its code and recorded outputs
    figure: str = Field(default="", max_length=120)  # "c0015" (its first figure) or "c0015:2"
    note: str = Field(default="", max_length=120)  # "n0007": a finding, verdict, decision... verbatim
    show: Show = "all"  # for a cell: code and outputs, only outputs, or only code
    caption: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _one(self) -> "Block":
        filled = [kind for kind in KINDS if getattr(self, kind).strip()]
        if len(filled) != 1:
            raise ValueError(f"a block has exactly one of {', '.join(KINDS)} (this one has "
                             f"{', '.join(filled) or 'none'})")
        return self

    @property
    def kind(self) -> str:
        return next(kind for kind in KINDS if getattr(self, kind).strip())


class ReportSpec(Frozen):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=8000)  # the question and the answer, first
    audience: str = Field(default="", max_length=200)  # e.g. "a professor", "the student"
    blocks: tuple[Block, ...] = Field(min_length=1, max_length=400)
    left_out: dict[str, str] = Field(default_factory=dict)  # journal ref -> why it is not in the report

    @field_validator("left_out")
    @classmethod
    def _left_out(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 400 or any(len(k) > 120 or len(v) > 1000 for k, v in value.items()):
            raise ValueError("left_out takes at most 400 refs, each with a reason of at most 1000 characters")
        return value


class ReportInfo(Frozen):
    folder: str  # relative to the project folder: reports/01-k562-table
    title: str
    built: str
    covers: str = ""  # the newest cell of the journal when the report was built
    warnings: int = 0
    notebook: str = ""  # relative to the project folder
    html: str = ""


class ReportAnswer(Frozen):
    status: Literal["draft", "published", "unchanged", "listed"]
    folder: str = ""
    notebook: str = ""
    html: str = ""
    warnings: tuple[str, ...] = ()
    covers: str = ""
    reports: tuple[ReportInfo, ...] = ()
    hint: str = ""
