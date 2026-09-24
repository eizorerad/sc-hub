---
name: resume
description: Pick up a project where the last session stopped (read this first in every new chat).
---
# Resuming a project

The chat forgets; the project does not. Before running anything:

1. `journal(project)` returns the hand-over (`handoff.md`, at most 120 lines), the
   checkpoint (disposition, next action, jobs it waits on) and the newest entries.
   Read the hand-over first: it says where the work stands and what comes next.
2. If the checkpoint says `waiting`, look at the listed jobs (`cluster()` shows your
   jobs). Do not start the same work again while they run.
3. If a result hints "the kernel restarted", variables are gone. Re-run the cells
   the hint lists as setup cells (or load the files they wrote), then continue.
4. Continue from the next action. Before you stop, update the hand-over with
   `handoff(project, text, disposition, next_action, waiting_jobs)`: what was done,
   what was found (with cell refs), what comes next. Evidence stays in the journal;
   the hand-over only says where to pick up.

Why: a new session that restarts from memory repeats work and loses decisions. In
the VCC2026 project, every agent shift started from a short hand-over file and the
F-numbered findings, and that is what kept a month of work coherent.
