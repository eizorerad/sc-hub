"""Codex: `codex exec --json <prompt>`, later turns `codex exec resume <thread id> --json <prompt>`.

The sandbox is read-only and approvals never asked: the agent's work goes through
the sc-hub MCP server (declared with -c, its tools pre-approved), so every step lands
in the journal. The thread id comes from the `thread.started` event.
"""

from __future__ import annotations

import json
import re

from .base import Engine, Outcome, Turn, classify

MISSING = re.compile(r"no (saved )?(session|conversation|thread|rollout)|(session|thread) .*not found", re.I)


def _toml(value: object) -> str:
    return json.dumps(value)  # JSON strings and arrays of strings are valid TOML


class Codex(Engine):
    name = "codex"

    def argv(self, binary: str, turn: Turn) -> list[str]:
        head = [binary, "exec", "resume", turn.session_id] if turn.session_id else [binary, "exec"]
        config = ['sandbox_mode="read-only"', 'approval_policy="never"']
        config += [f"model={_toml(turn.model)}"] if turn.model else []
        config += [f"model_reasoning_effort={_toml(turn.effort)}"] if turn.effort else []
        if turn.mcp is not None:
            env = ", ".join(f"{key} = {_toml(value)}" for key, value in turn.mcp.env)
            config += [f"mcp_servers.schub.command={_toml(turn.mcp.command)}",
                       f"mcp_servers.schub.args={_toml(list(turn.mcp.args))}",
                       f"mcp_servers.schub.env={{ {env} }}",
                       "mcp_servers.schub.startup_timeout_sec=60", "mcp_servers.schub.tool_timeout_sec=180",
                       'mcp_servers.schub.default_tools_approval_mode="approve"']
        options = [part for item in config for part in ("-c", item)]
        return [*head, "--json", "--skip-git-repo-check", *options, turn.prompt]

    def parse(self, stdout: str, stderr: str, returncode: int) -> Outcome:
        thread, messages, errors, completed, usage = None, [], [], False, {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "thread.started":
                thread = event.get("thread_id")
            elif kind == "turn.completed":
                completed, usage = True, event.get("usage") or {}
            elif kind == "turn.failed":
                errors.append(str((event.get("error") or {}).get("message", "turn failed")))
            elif kind == "error":
                errors.append(str(event.get("message", "error")))
            elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
                messages.append(str(event["item"].get("text", "")))
        ok = returncode == 0 and completed  # an "error" event before completion can be a retried reconnect
        error = "" if ok else ("\n".join(errors) or stderr.strip() or f"exit code {returncode}")[-2000:]
        return Outcome(self.name, classify(ok, error, bool(MISSING.search(error))), session_id=thread,
                       text=(messages[-1] if messages else "")[-4000:], error=error, returncode=returncode,
                       details={"usage": usage})
