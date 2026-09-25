"""Codex: `codex exec --json <prompt>`, later turns `codex exec resume <thread id> --json <prompt>`.

Its own tools are off (shell, browser, computer use, apps, sub-agents; the sandbox is
read-only besides) and approvals are never asked: the agent's work goes through the
sc-hub MCP server (declared with -c, its tools pre-approved), so every step lands in
the journal. The thread id comes from the `thread.started` event.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

from .base import Engine, Outcome, Turn, classify

# Codex's own tools, off: a read-only sandbox still lets its shell read any file of the account (other
# engines' logins included). Names from `codex features list` (codex-cli 0.155).
BUILT_IN_TOOLS = ("shell_tool", "browser_use", "browser_use_external", "computer_use", "in_app_browser", "apps",
                  "multi_agent", "image_generation")
# a thread that cannot go on: lost, or too long to resume (a new one starts from the hand-over)
MISSING = re.compile(r"no (saved )?(session|conversation|thread|rollout)|(session|thread) .*not found|"
                     r"context.?length.?exceeded|context window|maximum context length", re.I)


def _toml(value: object) -> str:
    return json.dumps(value)  # JSON strings and arrays of strings are valid TOML


class Codex(Engine):
    name = "codex"

    def environment(self, env: dict[str, str] | None) -> dict[str, str] | None:
        """Codex's SQLite files in a folder per host: the home folder is on NFS, shared by every node, and SQLite
        on NFS from several hosts at once corrupts or locks up (the pilot owner's wrapper does the same)."""
        env = dict(os.environ if env is None else env)
        if not env.get("CODEX_SQLITE_HOME"):
            parent = Path(env.get("HOME") or Path.home()) / ".codex-sqlite"
            folder = parent / socket.gethostname().split(".")[0]
            try:  # private: Codex keeps its session logs there
                parent.mkdir(mode=0o700, exist_ok=True)
                folder.mkdir(mode=0o700, exist_ok=True)
                env["CODEX_SQLITE_HOME"] = str(folder)
            except OSError:
                pass  # Codex then uses its default
        return env

    def argv(self, binary: str, turn: Turn) -> list[str]:
        head = [binary, "exec", "resume", turn.session_id] if turn.session_id else [binary, "exec"]
        config = ['sandbox_mode="read-only"', 'approval_policy="never"']
        config += [f"features.{name}=false" for name in BUILT_IN_TOOLS]  # the sc-hub MCP tools are its only tools
        config += [f"model={_toml(turn.model)}"] if turn.model else []
        config += [f"model_reasoning_effort={_toml(turn.effort)}"] if turn.effort else []
        if turn.mcp is not None:
            env = ", ".join(f"{_toml(key)} = {_toml(value)}" for key, value in turn.mcp.env)
            config += [f"mcp_servers.schub.command={_toml(turn.mcp.command)}",
                       f"mcp_servers.schub.args={_toml(list(turn.mcp.args))}",
                       f"mcp_servers.schub.env={{ {env} }}",
                       "mcp_servers.schub.startup_timeout_sec=60", "mcp_servers.schub.tool_timeout_sec=180",
                       'mcp_servers.schub.default_tools_approval_mode="approve"']
        else:
            config += ["mcp_servers={}"]  # a probe: none of the user's own MCP servers
        options = [part for item in config for part in ("-c", item)]
        return [*head, "--json", "--skip-git-repo-check", *options, turn.prompt]

    def parse(self, stdout: str, stderr: str, returncode: int) -> Outcome:
        thread, messages, errors, completed, usage, tools = None, [], [], False, {}, 0
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
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
            elif kind == "item.completed" and (event.get("item") or {}).get("type") not in (None, "reasoning"):
                tools += 1  # an MCP tool call (or another action): the turn did work even if it then failed
        ok = returncode == 0 and completed  # an "error" event before completion can be a retried reconnect
        error = "" if ok else ("\n".join(errors) or stderr.strip() or f"exit code {returncode}")[-2000:]
        return Outcome(self.name, classify(ok, error, bool(MISSING.search(error))), session_id=thread,
                       text=(messages[-1] if messages else "")[-4000:], error=error, returncode=returncode,
                       details={"usage": usage, "tool_calls": tools})
