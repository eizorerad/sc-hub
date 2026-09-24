"""The lab agent's engines: Claude Code and Codex behind one contract, a policy only
the owner changes, pauses after a usage limit, and guards that enforce the policy.

    policy.py    engine-policy.json: codex-only | claude-only | mixed, models, grants
    cooldown.py  "usage limit" -> the engine pauses until its reset
    base.py      the contract: argv, run with a time limit, parse, classify
    claude.py    claude -p --output-format json [--resume <id> | --session-id <id>]
    codex.py     codex exec [resume <id>] --json
    guard.py     the only way the lab agent starts an engine (bench/guard/{claude,codex})
    probe.py     "reply exactly OK" per engine, for the watchdog and the cluster overview
"""
