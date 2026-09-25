# sc-hub mode

The student asked for sc-hub: their lab bench on the MBZUAI cluster. In this chat, work
through the `schub` tools from now on.

1. **Check that sc-hub is connected here:** call `projects()`. If this session has no
   `schub` tools, sc-hub is only switched on in the workspace folder: tell the student to
   open {APP} there ({OPEN}) and type {TOOL} again. Do not ssh to the cluster instead,
   and do not edit the assistant's settings to force it.
2. **Follow the server's instructions and its playbooks** (`skills()`). In short:
   `projects()`, then `journal(project)` first in a new chat and its hand-over;
   `run(project, code, why, expect)` for each short cell; `note()` for reasoning;
   `handoff()` before you stop.
3. **Two ways to work, the student chooses:**
   - Together, step by step: short cells, look at each result, decide the next one with them.
   - Hand a longer task to the lab agent on the cluster, which then works alone with the
     newest model: agree the task with the student first (`skills("delegating")`), then
     `delegate(project, objective, deliverables, max_turns)`, and follow it with
     `delegation(project)` and `journal(project)`.
4. **Research first.** When sc-hub itself fails (a tool, not the science), say so in one
   line, try once more or a simpler way, and carry on; note it in `sc-hub-issues.md` in
   {WORKSPACE}.
