"""Who is calling: a profile per MCP client, from the clientInfo it sends.

Codex, Claude Code, Claude Desktop and ChatGPT differ in how long they wait for a
tool call and how much output they take well. Everything the bench says goes
through MCP, so the profile only tunes these limits; the tools are the same for all.
SCHUB_CLIENT_PROFILE overrides the detection (e.g. for a client that sends no name).
"""

from __future__ import annotations

import os
from typing import Mapping

from ..state import Frozen
from .models import Actor


class ClientProfile(Frozen):
    name: str
    run_wait_s: float  # how long `run` / `wait` hold the call before answering "running"
    max_output_chars: int  # text of all outputs in one answer
    mcp_apps: bool = False  # can render MCP Apps (ui://) cards
    elicitation: bool = False  # can ask the human directly
    inline_images: bool = False  # figures go into the answer (the model sees its plots)


PROFILES: dict[str, ClientProfile] = {
    "claude-code": ClientProfile(name="claude-code", run_wait_s=35, max_output_chars=8000, inline_images=True),
    "claude-desktop": ClientProfile(name="claude-desktop", run_wait_s=35, max_output_chars=6000, mcp_apps=True,
                                    inline_images=True),
    "codex": ClientProfile(name="codex", run_wait_s=45, max_output_chars=8000),  # installer sets tool_timeout_sec=180
    "chatgpt": ClientProfile(name="chatgpt", run_wait_s=30, max_output_chars=6000, mcp_apps=True),
    "unknown": ClientProfile(name="unknown", run_wait_s=25, max_output_chars=4000),
}


def detect(client_name: str) -> str:
    name = client_name.lower()
    if "codex" in name:
        return "codex"
    if "claude-code" in name or "claude_code" in name:
        return "claude-code"
    if "claude" in name:
        return "claude-desktop"
    if "chatgpt" in name or "openai" in name:
        return "chatgpt"
    return "unknown"


def profile_for(client_name: str, elicitation: bool = False, env: Mapping[str, str] | None = None) -> ClientProfile:
    env = os.environ if env is None else env
    forced = env.get("SCHUB_CLIENT_PROFILE", "")
    profile = PROFILES.get(forced) or PROFILES[detect(client_name)]
    return profile.model_copy(update={"elicitation": elicitation}) if elicitation else profile


def actor_for(client_name: str, client_version: str, env: Mapping[str, str] | None = None) -> Actor:
    """The chat client, or the lab agent: its goal job starts this server with SCHUB_LAB_AGENT_* set."""
    env = os.environ if env is None else env
    client, version = (client_name or "unknown")[:200], (client_version or "")[:200]
    engine = env.get("SCHUB_LAB_AGENT_ENGINE", "")
    if not engine:
        return Actor(kind="chat", client=client, client_version=version)
    return Actor(kind="lab_agent", client=client, client_version=version, engine=engine[:200],
                 model=env.get("SCHUB_LAB_AGENT_MODEL", "")[:200], effort=env.get("SCHUB_LAB_AGENT_EFFORT", "")[:200],
                 session_id=env.get("SCHUB_LAB_AGENT_SESSION", "")[:200])
