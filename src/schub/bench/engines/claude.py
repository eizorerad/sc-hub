"""Claude Code: `claude -p <prompt> --output-format json`, the sc-hub MCP server only.

New session: --session-id <uuid> (the id is known before the turn, so the journal's
actor can carry it); later turns: --resume <uuid>. Built-in tools are off
(--tools ""); the agent works through the bench's MCP tools alone.
"""

from __future__ import annotations

import json
import re

from .base import Engine, Outcome, Turn, classify

MISSING = re.compile(r"no conversation found|session .* (not found|does not exist)|already in use", re.I)


class Claude(Engine):
    name = "claude"

    def argv(self, binary: str, turn: Turn) -> list[str]:
        argv = [binary, "-p", turn.prompt, "--output-format", "json", "--tools", ""]
        if turn.mcp is not None:
            server = {"command": turn.mcp.command, "args": list(turn.mcp.args), "env": dict(turn.mcp.env)}
            argv += ["--mcp-config", json.dumps({"mcpServers": {"schub": server}}), "--strict-mcp-config",
                     "--allowedTools", "mcp__schub__*"]
        argv += ["--model", turn.model] if turn.model else []
        argv += ["--effort", turn.effort] if turn.effort else []
        if turn.session_id:
            argv += ["--resume", turn.session_id]
        elif turn.new_session_id:
            argv += ["--session-id", turn.new_session_id]
        return argv

    def parse(self, stdout: str, stderr: str, returncode: int) -> Outcome:
        result = _last_json(stdout)
        if result is None:
            error = (stderr or stdout).strip()[-2000:] or f"exit code {returncode} and no result"
            return Outcome(self.name, classify(False, error, bool(MISSING.search(error))), error=error,
                           returncode=returncode)
        text = str(result.get("result") or "")
        ok = returncode == 0 and result.get("subtype") == "success" and not result.get("is_error")
        error = "" if ok else (text or (stderr or "").strip())[-2000:]
        return Outcome(self.name, classify(ok, error, bool(MISSING.search(error))),
                       session_id=result.get("session_id"), text=text[-4000:], error=error,
                       cost_usd=result.get("total_cost_usd"), turns=result.get("num_turns"), returncode=returncode,
                       details={"models": sorted((result.get("modelUsage") or {}).keys())})


def _last_json(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("type") == "result":
            return value
    try:
        value = json.loads(stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
