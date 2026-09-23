"""Skills: markdown playbooks the agent reads through MCP (`skills` tool), so every
client (Codex, Claude Code, Claude Desktop, ChatGPT) gets the same guidance."""

from __future__ import annotations

import re
from pathlib import Path

from ..state import Frozen

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
NAME = re.compile(r"^[a-z0-9_]{1,40}$")
FRONT = re.compile(r"^---\n(?P<head>.*?)\n---\n(?P<body>.*)$", re.S)


class SkillError(ValueError):
    pass


class SkillInfo(Frozen):
    name: str
    description: str


def _parse(text: str) -> tuple[dict[str, str], str]:
    match = FRONT.match(text)
    if match is None:
        return {}, text
    head = {}
    for line in match["head"].splitlines():
        key, _, value = line.partition(":")
        head[key.strip()] = value.strip()
    return head, match["body"]


def list_skills(folder: Path = SKILLS_DIR) -> list[SkillInfo]:
    found = []
    for path in sorted(folder.glob("*.md")):
        head, _ = _parse(path.read_text())
        found.append(SkillInfo(name=path.stem, description=head.get("description", "")))
    return found


def get_skill(name: str, folder: Path = SKILLS_DIR) -> str:
    if not NAME.fullmatch(name):
        raise SkillError(f"'{name}' is not a skill name")
    path = folder / f"{name}.md"
    if not path.is_file():
        available = ", ".join(s.name for s in list_skills(folder))
        raise SkillError(f"no skill '{name}'; available: {available}")
    return _parse(path.read_text())[1].strip()
