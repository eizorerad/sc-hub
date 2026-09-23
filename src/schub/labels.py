"""Branch labels: tags, pinned, archived. Bookkeeping for students with many
branches, kept apart from the branch specs so labelling never makes a revision.

    projects/<project>/branch-labels.yaml   {branch: {tags: [...], pinned: bool, archived: bool}}
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .locking import exclusive
from .projects import ProjectError, ProjectStore
from .state import Frozen

LABELS_FILE = "branch-labels.yaml"
TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
MAX_TAGS = 12


class BranchLabel(Frozen):
    tags: tuple[str, ...] = ()
    pinned: bool = False
    archived: bool = False


class LabelStore:
    def __init__(self, projects: ProjectStore) -> None:
        self.projects = projects

    def _path(self, project: str) -> Path:
        return self.projects.require(project) / LABELS_FILE

    def _raw(self, project: str) -> dict[str, Any]:
        """The file as it is; a hand edit that broke it is an error, never an empty file
        (updating from {} would silently drop every other branch's labels)."""
        path = self._path(project)
        if not path.is_file():
            return {}
        try:
            raw = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ProjectError(f"{path} is not valid YAML ({exc.__class__.__name__}); fix it by hand") from exc
        if raw is not None and not isinstance(raw, dict):
            raise ProjectError(f"{path} should map branch names to labels")
        return raw or {}

    def all(self, project: str) -> dict[str, BranchLabel]:
        try:
            raw = self._raw(project)
        except (OSError, ProjectError):
            return {}  # the dashboard shows the branches without labels
        found = {}
        for branch, data in raw.items():
            try:
                found[str(branch)] = BranchLabel.model_validate(data or {})
            except ValueError:
                continue  # a hand-edited entry that no longer parses: ignore, do not fail the page
        return found

    def update(self, project: str, branch: str, add_tags: tuple[str, ...] = (), remove_tags: tuple[str, ...] = (),
               pinned: bool | None = None, archived: bool | None = None) -> BranchLabel:
        if not self.projects.branch_exists(project, branch):
            raise ProjectError(f"no branch '{branch}' in '{project}'")
        bad = [t for t in add_tags if not TAG.fullmatch(t)]
        if bad:
            raise ProjectError(f"invalid tag(s) {bad}: lowercase letters, digits, '-' or '_', up to 31 characters")
        path = self._path(project)
        with exclusive(path.with_name(f".{path.name}.lock")):  # concurrent label_branch calls
            raw = self._raw(project)
            try:
                current = BranchLabel.model_validate(raw.get(branch) or {})
            except ValueError:
                current = BranchLabel()
            tags = tuple(t for t in dict.fromkeys((*current.tags, *add_tags)) if t not in set(remove_tags))
            if len(tags) > MAX_TAGS:
                raise ProjectError(f"at most {MAX_TAGS} tags per branch")
            updated = BranchLabel(tags=tags, pinned=current.pinned if pinned is None else pinned,
                                  archived=current.archived if archived is None else archived)
            text = yaml.safe_dump({**raw, branch: updated.model_dump(mode="json")}, sort_keys=True)
            fd, partial = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(text)
                os.chmod(partial, 0o644)  # like the other project files (mkstemp makes 0600)
                os.replace(partial, path)
            except BaseException:
                Path(partial).unlink(missing_ok=True)
                raise
        return updated
