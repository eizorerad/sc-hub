"""Fake Claude Code and Codex CLIs for the engine and lab-agent tests.

Each fake appends its argv (one JSON line) to $FAKE_LOG and answers by $FAKE_<ENGINE>:
ok, limit, missing (session not found), slow (sleeps), garbage. A file $FAKE_<ENGINE>_SCRIPT
may hold one mode per line, consumed in order (a turn per line).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CLAUDE = r'''
mode = next_mode("CLAUDE")
args = sys.argv[1:]
session = args[args.index("--resume") + 1] if "--resume" in args else (
    args[args.index("--session-id") + 1] if "--session-id" in args else "11111111-2222-3333-4444-555555555555")
if mode == "slow":
    time.sleep(60)
if mode == "garbage":
    print("not json"); sys.exit(1)
if mode == "missing":
    print("No conversation found with session ID: " + session, file=sys.stderr); sys.exit(1)
prompt = args[args.index("-p") + 1]
text = "OK" if "exactly" in prompt else "did the step"
error = mode in ("limit", "limitwork")  # refused at once, or after some work
print(json.dumps({"type": "result", "subtype": "success", "is_error": error,
                  "result": "You've hit your limit · resets 3pm (Asia/Dubai)" if error else text,
                  "session_id": session, "total_cost_usd": 0.0 if mode == "limit" else 0.01,
                  "num_turns": 1 if mode == "limit" else 5, "modelUsage": {"claude-fake": {}}}))
sys.exit(1 if error else 0)
'''

CODEX = r'''
mode = next_mode("CODEX")
args = sys.argv[1:]
thread = args[args.index("resume") + 1] if "resume" in args else "019a0000-0000-7000-8000-00000000c0de"
if mode == "slow":
    time.sleep(60)
if mode == "missing":
    print(json.dumps({"type": "error", "message": "No saved session found with ID " + thread})); sys.exit(1)
print(json.dumps({"type": "thread.started", "thread_id": thread}))
print(json.dumps({"type": "turn.started"}))
if mode == "limit":
    print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 2099-01-01T10:00:00Z."}}))
    sys.exit(1)
prompt = args[-1]
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message",
                  "text": "OK" if "exactly" in prompt else "did the step"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}))
'''

HEADER = r'''
import json, os, sys, time
from pathlib import Path
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"engine": ENGINE, "argv": sys.argv[1:], "pid": os.getpid(),
                          "env": {k: v for k, v in os.environ.items() if k.startswith("SCHUB_LAB")}}) + "\n")

def next_mode(name):
    script = os.environ.get(f"FAKE_{name}_SCRIPT")
    if script and Path(script).exists():
        lines = Path(script).read_text().splitlines()
        if lines:
            Path(script).write_text("\n".join(lines[1:]))
            return lines[0].strip()
    return os.environ.get(f"FAKE_{name}", "ok")
'''


def install(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for engine, body in (("claude", CLAUDE), ("codex", CODEX)):
        path = folder / engine
        path.write_text(f"#!{sys.executable}\nENGINE = {engine!r}\n{HEADER}\n{body}")
        path.chmod(0o755)
        paths[engine] = path
    return paths


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
