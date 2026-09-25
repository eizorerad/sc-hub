"""The sc-hub MCP server in the student's assistants: Codex, Claude Code, Claude Desktop (whichever exist).

sc-hub is there only when the student asks for it, so their other work stays free of it:
- Codex: the server is defined but off; the workspace's own .codex/config.toml turns it on
  there (a trusted folder), and the skill $schub explains the mode.
- Claude Code: the server is registered for the workspace folder only (scope "local"), and
  the skill /schub explains the mode.
- Claude Desktop: a chat app that reaches the cluster only through sc-hub: connected as before.
"""

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


def codex_home(paths: Paths) -> Path:
    return Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") and not paths.custom else paths.home / ".codex"


def codex(paths: Paths, remote_root: str, workspace_dir: Path, repo: Path) -> str:
    folder = codex_home(paths)
    config = folder / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    text = strip_block(config.read_text() if config.exists() else "")
    if "[mcp_servers.schub]" in text:
        return "Codex: your config already defines [mcp_servers.schub] by hand; left as it is"
    _, args = mcp_command(paths, remote_root)
    lines = [BEGIN, "[mcp_servers.schub]", 'command = "ssh"', f"args = {json.dumps(args)}",
             "startup_timeout_sec = 60", "tool_timeout_sec = 180",
             "# The bench records every call in the project journal; asking before each cell would stall.",
             'default_tools_approval_mode = "approve"',
             "# Off everywhere but the sc-hub workspace, whose .codex/config.toml turns it on ($schub explains it).",
             "enabled = false"]
    trust = f"[projects.{json.dumps(str(workspace_dir))}]"
    if trust not in text:  # (a table the student already has must not be defined twice)
        lines += ["", trust, 'trust_level = "trusted"']
    config.write_text((text.rstrip("\n") + "\n\n" if text.strip() else "") + "\n".join(lines + [END]) + "\n")
    local = workspace_dir / ".codex" / "config.toml"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("# sc-hub's workspace: its MCP server is on here (it is off elsewhere).\n"
                     "[mcp_servers.schub]\nenabled = true\n")
    skill = folder / "skills" / "schub"
    (skill / "agents").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(skill_text(repo, workspace_dir, "codex"))
    (skill / "agents" / "openai.yaml").write_text(
        'interface:\n  display_name: "sc-hub"\n  short_description: "Your lab bench on the MBZUAI cluster"\n'
        '  default_prompt: "Use $schub to continue my project on the cluster."\n'
        "policy:\n  allow_implicit_invocation: false\n")
    return f"Codex: connected in {workspace_dir} ($schub; also Codex in the ChatGPT desktop app)"


def skill_text(repo: Path, workspace_dir: Path, app: str) -> str:
    """The /schub (Claude Code) or $schub (Codex) skill: sc-hub mode, asked for by the student."""
    claude = app == "claude"
    head = ["---", "name: schub",
            "description: sc-hub mode, the student's lab bench on the MBZUAI cluster (projects, cells, journal, "
            "lab agent). Only when the student asks for sc-hub."]
    head += ["disable-model-invocation: true"] if claude else []
    body = (repo / "templates" / "skill-schub.md").read_text().format(
        APP="Claude Code" if claude else "Codex", TOOL="/schub" if claude else "$schub", WORKSPACE=workspace_dir,
        OPEN=f"cd {workspace_dir} && claude" if claude else f"codex -C {workspace_dir}")
    return "\n".join(head + ["---", ""]) + body


def claude_code(paths: Paths, remote_root: str, workspace_dir: Path, repo: Path) -> str:
    claude = shutil.which("claude")
    if claude is None:
        return ""
    command, args = mcp_command(paths, remote_root)
    env = {**os.environ, **({"HOME": str(paths.home), "USERPROFILE": str(paths.home)} if paths.custom else {})}
    for scope in ("user", "local"):  # an older setup registered it for every folder; a re-run replaces its own
        subprocess.run([claude, "mcp", "remove", "schub", "-s", scope], capture_output=True, env=env, timeout=60,
                       cwd=workspace_dir)
    done = subprocess.run([claude, "mcp", "add", "-s", "local", "schub", "--", command, *args], capture_output=True,
                          text=True, env=env, timeout=60, cwd=workspace_dir)
    if done.returncode != 0:
        return f"Claude Code: could not register ({done.stderr.strip()[-160:]}); in {workspace_dir} run: " \
               f"claude mcp add -s local schub -- {command} {' '.join(args)}"
    config_dir = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") and not paths.custom \
        else paths.home / ".claude"
    skill = config_dir / "skills" / "schub"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(skill_text(repo, workspace_dir, "claude"))
    return f"Claude Code: connected in {workspace_dir} (/schub)"


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
    template = (repo / "templates" / "AGENTS.workspace.md").read_text().replace(
        "(sc-hub setup folder: unknown, ask the student)", f"(sc-hub setup folder: {repo})")
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
