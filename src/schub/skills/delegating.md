---
name: delegating
description: Hand a task to the lab agent on the cluster (delegate): agree it with the student, write it so another agent can do it alone, follow it, stop it.
---
# Handing work to the lab agent

The lab agent is Claude Code or Codex running on the cluster, in Slurm slices about an
hour apart, until the task is done or its turns are spent. It starts from the task you
write and the journal; it cannot ask the student anything. What it does is recorded in
the journal, so you and the student can follow it.

## When

- Long or many-step work: a whole QC and analysis, a reproduction, a sweep of models.
- Not for a quick look or a question the student is thinking through with you: run
  cells yourself for that.

## First agree the task with the student

Settle these before delegating, in the chat:

1. **The question**, in one sentence, and why it matters to them.
2. **The data**: which dataset (from `datasets()`), or where to get it, and which cells
   or files already exist in the project.
3. **What counts as done**: the tables, figures, numbers or report that must exist at the
   end, and the checks they must pass.
4. **Limits**: GPU time, downloads, anything not to touch, and the budget (`max_turns`;
   a turn is up to about 80 minutes of work).

## Write the objective for someone who was not here

- The whole task in plain words: the question, the data with paths, the method if the
  student chose one, what is already known (cite cells: `project#c0012`).
- The deliverables, concretely: file names, columns, figures, the report.
- The decisions already made with the student and their reasons, so it does not reopen them.
- What to do if something is unclear: hand over as "blocked" with the question, rather than guess.

## Hand it over and follow it

```
delegate(project, objective, deliverables="...", max_turns=20)
delegation(project)            # its state, turns used, next action, last events
journal(project)               # what it did, cell by cell
delegation(project, stop=True) # stop it; the journal keeps everything
```

- mode "free" (the default) lets it use its own shell, files and subagents; it writes only
  in the project's work/ folder. "bench" limits it to the sc-hub tools.
- While it works on a project, a new task there is refused; `replace=True` starts the new
  one instead (the same objective handed over again keeps its budget).
- It runs with the newest model at high effort, and stands down while Claude's weekly
  window is 80% full, so the student's own chats keep their share.
- When it hands over as "blocked", read its question in the journal, settle it with the
  student, and delegate again with the answer in the objective.
