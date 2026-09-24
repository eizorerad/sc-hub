"""Where reports live, and their HTML copy.

    projects/<p>/reports/
      README.md          the published reports, one line each (rewritten on each publish)
      last-publish.json  when a publish was last asked for (also when it changed nothing)
      .numbers/          report numbers taken (exclusive create: two publishes never share one)
      draft/             the latest draft, replaced by every build
      01-k562-table/     a published report, never changed afterwards
        report.ipynb     the notebook, figures inside
        report.html      the same without code (when nbconvert is installed)
        spec.json        what the author asked for
        report.json      when and by whom, what it covers, its warnings, sha256 of the notebook

A new publish takes the next number; publishing the same spec over the same journal
again keeps the existing report instead of adding a copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from .fsio import read_json, write_json_atomic
from .models import Actor
from .report_build import Built
from .report_spec import ReportAnswer, ReportInfo, ReportSpec

PUBLISHED = re.compile(r"^(?P<number>\d{2,4})-[a-z0-9-]{1,40}$")
NO_HTML = "no HTML copy: nbconvert is not installed where sc-hub runs (the notebook is complete)"
HINTS = {
    "draft": "A draft: read the warnings (advice, not failures), change the spec where it helps, then call "
             "report(project, spec, publish=true).",
    "published": "Published; the dashboard shows it on the project's Journal page.",
    "unchanged": "Nothing changed since this report was published: it stays as it is.",
}


def slug(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")[:40].strip("-") or "report"


def to_html(notebook: dict[str, Any]) -> str | None:
    """The notebook as a page without code, or None where nbconvert is missing or fails."""
    try:
        import nbformat
        from nbconvert import HTMLExporter
    except ImportError:
        return None
    try:
        exporter = HTMLExporter(template_name="lab", exclude_input=True, exclude_input_prompt=True,
                                exclude_output_prompt=True, sanitize_html=True)  # the prose is an agent's
        node = nbformat.reads(json.dumps(notebook), as_version=4)  # joins the line lists nbconvert cannot read
        body, _ = exporter.from_notebook_node(node)
        return body
    except Exception:  # noqa: BLE001 - the notebook is the report; the page is a convenience
        return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class ReportStore:
    def __init__(self, project_dir: Path) -> None:
        self.folder = project_dir / "reports"

    def published(self) -> list[ReportInfo]:
        found = []
        for path in self.folder.iterdir() if self.folder.is_dir() else []:
            match = PUBLISHED.fullmatch(path.name)
            meta = read_json(path / "report.json") if match else None
            if meta:
                found.append((int(match["number"]), self._info(path, meta)))
        return [info for _, info in sorted(found, key=lambda pair: pair[0])]

    def latest(self) -> ReportInfo | None:
        reports = self.published()
        return reports[-1] if reports else None

    def published_since(self, since: str) -> ReportInfo | None:
        """The newest report, if one was published (or confirmed unchanged) at or after `since`."""
        latest = self.latest()
        asked = str((read_json(self.folder / "last-publish.json") or {}).get("at", ""))
        return latest if latest is not None and max(latest.built, asked) >= since else None

    def _info(self, path: Path, meta: dict[str, Any]) -> ReportInfo:
        name = path.name
        return ReportInfo(folder=f"reports/{name}", title=str(meta.get("title", "")), built=str(meta.get("built", "")),
                          covers=str(meta.get("covers", "")), warnings=len(meta.get("warnings", [])),
                          notebook=f"reports/{name}/report.ipynb",
                          html=f"reports/{name}/report.html" if (path / "report.html").is_file() else "")

    # ---- writing --------------------------------------------------------------------------

    def draft(self, built: Built, spec: ReportSpec, actor: Actor, when: str) -> ReportAnswer:
        staged, meta = self._stage(built, spec, actor, when)
        target = self.folder / "draft"
        for _ in range(3):  # another build may put its draft there in between
            try:
                staged.rename(target)
                return self._answer("draft", target, meta)
            except OSError:
                self._set_aside(target)
        shutil.rmtree(staged, ignore_errors=True)
        raise OSError(f"could not replace {target}")

    def _set_aside(self, folder: Path) -> None:
        trash = self.folder / f".old-{secrets.token_hex(4)}"
        try:
            folder.rename(trash)
        except OSError:
            return  # gone already
        shutil.rmtree(trash, ignore_errors=True)

    def publish(self, built: Built, spec: ReportSpec, actor: Actor, when: str) -> ReportAnswer:
        latest = self.latest()
        spec_sha = _sha(spec.model_dump_json())
        if latest is not None:
            meta = read_json(self.folder / latest.folder.removeprefix("reports/") / "report.json") or {}
            if meta.get("spec_sha256") == spec_sha and meta.get("covers") == built.covers:
                write_json_atomic(self.folder / "last-publish.json", {"at": when, "folder": latest.folder})
                return self._answer("unchanged", self.folder / latest.folder.removeprefix("reports/"), meta)
        staged, meta = self._stage(built, spec, actor, when)
        try:
            target = self.folder / f"{self._claim():02d}-{slug(spec.title)}"
            staged.rename(target)
        except OSError:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        for path in target.iterdir():
            path.chmod(0o444)  # a published report is not edited; a new one is published instead
        write_json_atomic(self.folder / "last-publish.json", {"at": when, "folder": f"reports/{target.name}"})
        self._set_aside(self.folder / "draft")  # published: the draft would only confuse
        self._index()
        return self._answer("published", target, meta)

    def _claim(self) -> int:
        """The next report number, taken by exclusive create (like journal ids)."""
        marks = self.folder / ".numbers"
        marks.mkdir(parents=True, exist_ok=True)
        taken = [int(m["number"]) for p in self.folder.iterdir() if (m := PUBLISHED.fullmatch(p.name))]
        taken += [int(p.name) for p in marks.iterdir() if p.name.isdigit()]
        number = max(taken, default=0) + 1
        while number < 10_000:
            try:
                os.close(os.open(marks / str(number), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                return number
            except FileExistsError:
                number += 1
        raise OSError(f"no free report number in {self.folder}")

    def _stage(self, built: Built, spec: ReportSpec, actor: Actor, when: str) -> tuple[Path, dict[str, Any]]:
        staged = self.folder / f".staging-{secrets.token_hex(4)}"
        staged.mkdir(parents=True)
        try:
            return staged, self._fill(staged, built, spec, actor, when)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise

    def _fill(self, staged: Path, built: Built, spec: ReportSpec, actor: Actor, when: str) -> dict[str, Any]:
        text = json.dumps(built.notebook, indent=1, ensure_ascii=False)
        (staged / "report.ipynb").write_text(text)
        html = to_html(built.notebook)
        if html is not None:
            (staged / "report.html").write_text(html)
        (staged / "spec.json").write_text(spec.model_dump_json(indent=1))
        meta = {"title": spec.title, "built": when, "by": actor.model_dump(mode="json"), "covers": built.covers,
                "cited": list(built.cited), "warnings": list(built.warnings) + ([] if html is not None else [NO_HTML]),
                "notebook_sha256": _sha(text), "spec_sha256": _sha(spec.model_dump_json()), "html": html is not None}
        write_json_atomic(staged / "report.json", meta)
        return meta

    def _answer(self, status: str, folder: Path, meta: dict[str, Any]) -> ReportAnswer:
        name = f"reports/{folder.name}"
        return ReportAnswer(status=status, folder=name, notebook=f"{name}/report.ipynb",  # type: ignore[arg-type]
                            html=f"{name}/report.html" if (folder / "report.html").is_file() else "",
                            warnings=tuple(meta.get("warnings", [])), covers=str(meta.get("covers", "")),
                            hint=HINTS[status])

    def _index(self) -> None:
        rows = [f"| [{r.folder.removeprefix('reports/')}]({r.folder.removeprefix('reports/')}/report.ipynb) "
                f"{r.title.replace('|', '/')} | {r.built[:16].replace('T', ' ')} | {r.covers} | {r.warnings} |"
                for r in self.published()]
        text = "\n".join(["# Reports", "", "Each folder is one published report; they are never changed.",
                          "", "| Report | Built (UTC) | Covers the journal to | Warnings |", "|---|---|---|---|",
                          *rows]) + "\n"
        temp = self.folder / ".README.md.tmp"
        temp.write_text(text)
        temp.replace(self.folder / "README.md")
