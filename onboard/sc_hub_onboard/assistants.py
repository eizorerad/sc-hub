"""The sc-hub MCP server in the student's assistants: Codex, Claude Code, Claude Desktop (whichever exist)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from .sshkit import ALIAS, BEGIN, END, Paths, strip_block


def mcp_command(paths: Paths, remote_root: str) -> tuple[str, list[str]]:
    """How an assistant starts the server: ssh with the key to the gate's schub-mcp."""
    args = ["-T"] + (["-F", str(paths.ssh_config)] if paths.custom else []) + ["-o", "BatchMode=yes", ALIAS,
                                                                              f"{remote_root}/bin/schub-mcp"]
    return shutil.which("ssh") or "ssh", args


def codex(paths: Paths, remote_root: str) -> str:
    folder = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") and not paths.custom else paths.home / ".codex"
    config = folder / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    text = strip_block(config.read_text() if config.exists() else "")
    if "[mcp_servers.schub]" in text:
        return "Codex: your config already defines [mcp_servers.schub] by hand; left as it is"
    _, args = mcp_command(paths, remote_root)
    block = "\n".join([BEGIN, "[mcp_servers.schub]", 'command = "ssh"', f"args = {json.dumps(args)}",
                       "startup_timeout_sec = 60", "tool_timeout_sec = 180",
                       "# The bench records every call in the project journal; asking before each cell would stall.",
                       'default_tools_approval_mode = "approve"', END]) + "\n"
    config.write_text((text.rstrip("\n") + "\n\n" if text.strip() else "") + block)
    return "Codex: connected (also Codex in the ChatGPT desktop app)"


def claude_code(paths: Paths, remote_root: str) -> str:
    claude = shutil.which("claude")
    if claude is None:
        return ""
    command, args = mcp_command(paths, remote_root)
    env = {**os.environ, **({"HOME": str(paths.home), "USERPROFILE": str(paths.home)} if paths.custom else {})}
    subprocess.run([claude, "mcp", "remove", "schub", "-s", "user"], capture_output=True, env=env, timeout=60)
    done = subprocess.run([claude, "mcp", "add", "-s", "user", "schub", "--", command, *args], capture_output=True,
                          text=True, env=env, timeout=60)
    if done.returncode != 0:
        return f"Claude Code: could not register ({done.stderr.strip()[-160:]}); run: claude mcp add -s user schub -- " \
               f"{command} {' '.join(args)}"
    return "Claude Code: connected"


def desktop_config(paths: Paths) -> Path:
    system = platform.system()
    if system == "Darwin":
        return paths.home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        base = Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") and not paths.custom else \
            paths.home / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    return paths.home / ".config" / "Claude" / "claude_desktop_config.json"


def desktop_installed(paths: Paths) -> bool:
    config = desktop_config(paths)
    apps = [Path("/Applications/Claude.app")] if platform.system() == "Darwin" and not paths.custom else []
    if platform.system() == "Windows" and os.environ.get("LOCALAPPDATA") and not paths.custom:
        apps.append(Path(os.environ["LOCALAPPDATA"]) / "AnthropicClaude")
    return config.parent.is_dir() or any(a.exists() for a in apps)


def claude_desktop(paths: Paths, remote_root: str) -> str:
    if not desktop_installed(paths):
        return ""
    config = desktop_config(paths)
    config.parent.mkdir(parents=True, exist_ok=True)
    backup = config.with_name(config.name + ".bak-schub")
    if config.exists() and not backup.exists():
        shutil.copy2(config, backup)  # what the student had before sc-hub touched it
    try:
        data = json.loads(config.read_text()) if config.exists() and config.read_text().strip() else {}
    except ValueError:
        return "Claude Desktop: its config is not valid JSON; add the schub server by hand"
    command, args = mcp_command(paths, remote_root)
    data.setdefault("mcpServers", {})["schub"] = {"command": command, "args": args}
    temp = config.with_name(config.name + ".schub-tmp")
    temp.write_text(json.dumps(data, indent=2))
    temp.replace(config)
    return "Claude Desktop: connected (restart it to load sc-hub)"


def workspace(paths: Paths, repo: Path) -> Path:
    """~/sc-hub-workspace: the assistants' instructions and the dashboard mirror."""
    folder = paths.workspace
    folder.mkdir(parents=True, exist_ok=True)
    template = (repo / "templates" / "AGENTS.workspace.md").read_text()
    for name in ("AGENTS.md", "CLAUDE.md"):
        (folder / name).write_text(template)
    names = ("schub-view.cmd", "schub-view.ps1", "schub-lab.cmd", "schub-lab.ps1") if os.name == "nt" else \
        ("schub-view", "schub-lab")
    for name in names:
        source = repo / "scripts" / name
        if source.exists():
            target = folder / name
            shutil.copy2(source, target)
            if os.name != "nt":
                target.chmod(0o755)
    return folder
