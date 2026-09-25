"""A free lab agent's own actions, recorded in the journal after each of its turns.

A free agent (goal.md `mode: free`) works with its engine's shell, file tools and subagents,
not only the bench's MCP tools (whose calls the journal already holds). What it did must still
be on the dashboard: after the turn, its transcript (Claude Code's stream-json, Codex's --json
events) becomes one journal cell: every command with its output, each file edit, subagent and
web lookup, the files it created or changed in the project, and what it said at the end.
Reads and searches are only counted. The full transcript stays in goal/state/runs/.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .clock import stamp
from .engines.base import Outcome
from .journal import Journal
from .models import Actor, CellEntry, FileChange, OutputItem

MAX_OUTPUT_CHARS = 1500  # of one command's output in the entry
MAX_ACTIONS = 200
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
LOOKS = ("Read", "Glob", "Grep", "TodoWrite", "LS")
SUBAGENTS = ("Task", "Agent")


@dataclass(frozen=True)
class Action:
    kind: str  # shell | edit | subagent | web | other
    what: str  # the command, the file, the task, the query
    output: str = ""
    failed: bool = False


@dataclass(frozen=True)
class Turn:
    actions: tuple[Action, ...]
    looks: int  # files read, searches
    bench_calls: int  # sc-hub tool calls (in the journal already)


def _events(stdout: str) -> Iterator[dict]:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def claude_turn(stdout: str) -> Turn:
    """Tool uses of the turn (subagents' included) paired with their results."""
    uses: dict[str, tuple[str, dict]] = {}
    order: list[str] = []
    results: dict[str, tuple[str, bool]] = {}
    for event in _events(stdout):
        blocks = (event.get("message") or {}).get("content") or []
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if event.get("type") == "assistant" and block.get("type") == "tool_use":
                uses[str(block.get("id"))] = (str(block.get("name", "")), block.get("input") or {})
                order.append(str(block.get("id")))
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                results[str(block.get("tool_use_id"))] = (_text(block.get("content")), bool(block.get("is_error")))
    actions, looks, bench = [], 0, 0
    for use_id in order:
        name, args = uses[use_id]
        output, failed = results.get(use_id, ("", False))
        if name == "Bash":
            actions.append(Action("shell", str(args.get("command", "")), output, failed))
        elif name in EDITS:
            actions.append(Action("edit", str(args.get("file_path") or args.get("notebook_path") or ""),
                                  output if failed else "", failed))
        elif name in SUBAGENTS:
            actions.append(Action("subagent", str(args.get("description") or args.get("prompt", ""))[:300], output,
                                  failed))
        elif name in ("WebFetch", "WebSearch"):
            actions.append(Action("web", str(args.get("url") or args.get("query") or ""), "", failed))
        elif name.startswith("mcp__schub__"):
            bench += 1
        elif name in LOOKS:
            looks += 1
        else:
            actions.append(Action("other", name, output, failed))
    return Turn(tuple(actions[:MAX_ACTIONS]), looks, bench)


def codex_turn(stdout: str) -> Turn:
    actions, bench = [], 0
    for event in _events(stdout):
        item = event.get("item") or {}
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "command_execution":
            code = item.get("exit_code")
            actions.append(Action("shell", str(item.get("command", "")), str(item.get("aggregated_output", "")),
                                  code not in (0, None)))
        elif kind == "file_change":
            for change in item.get("changes") or []:
                if isinstance(change, dict):
                    actions.append(Action("edit", f"{change.get('path', '')} ({change.get('kind', 'changed')})"))
        elif kind == "web_search":
            actions.append(Action("web", str(item.get("query", ""))))
        elif kind == "mcp_tool_call":
            bench += 1
    return Turn(tuple(actions[:MAX_ACTIONS]), 0, bench)


def parse(engine: str, stdout: str) -> Turn:
    return claude_turn(stdout) if engine == "claude" else codex_turn(stdout)


def _line(action: Action) -> str:
    if action.kind == "shell":
        return f"$ {action.what}"
    return f"# {action.kind}: {action.what}"


def _outputs(actions: tuple[Action, ...]) -> tuple[OutputItem, ...]:
    items = []
    for action in actions:
        if not action.output and not action.failed:
            continue
        text = action.output or "(failed)"
        cut = max(0, len(text) - MAX_OUTPUT_CHARS)
        items.append(OutputItem(kind="stream", name="stderr" if action.failed else "stdout",
                                text=f"{_line(action)}\n{text[:MAX_OUTPUT_CHARS]}", truncated=cut))
    return tuple(items)


def record(journal: Journal, turn: Turn, files: tuple[FileChange, ...], outcome: Outcome, actor: Actor,
           transcript: Path, started: str) -> str | None:
    """One final journal cell for the turn's own actions; None when it used only the sc-hub tools."""
    if not turn.actions and not files:
        return None
    cid = journal.allocate("c")
    header = (f"# {actor.engine}'s own actions in this lab-agent turn (free mode), recorded by sc-hub after the turn.\n"
              f"# Also: {turn.looks} file reads or searches, {turn.bench_calls} sc-hub tool calls (in the journal).\n"
              f"# Full transcript: {transcript}\n")
    summary = " ".join((outcome.text or "").split())[:300]
    ok = outcome.status == "ok"
    entry = CellEntry(
        ref=journal.ref(cid), project=journal.project, cid=cid,
        why=f"The lab agent's own work in this turn: {summary}" if summary else "The lab agent's own work in this turn",
        expect="recorded after the turn: its commands with their output, and the files it changed",
        code=header + "\n".join(_line(a) for a in turn.actions), actor=actor, created=started, started=started,
        finished=stamp(), status="ok" if ok else "error", outputs=_outputs(turn.actions), files=files,
        message="" if ok else f"the turn ended as {outcome.status}: {(outcome.error or '')[-300:]}",
    )
    journal.write_cell(entry)
    return cid
