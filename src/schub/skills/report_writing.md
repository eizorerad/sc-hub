---
name: report_writing
description: Turn a finished study into a report notebook for a student or a professor (the report tool): structure, what to show, how to cite the journal.
---
# Writing the report of a study

The journal is the protocol: every step, in order, dead ends included. The report is
the story a reader follows in 10–20 minutes. You write the prose and choose what to
show; sc-hub takes code, outputs, figures and notes verbatim from the journal, so
nothing in the report can drift from what really ran.

## 1. Read before writing

`journal(project)`: the hand-over, then the findings, verdicts, decisions and
mistakes (`journal(project, kinds=["finding", "verdict", "decision", "error"])`), then
the cells they cite. Note which cells hold the key numbers and figures.

## 2. A shape that works (adapt it to the request)

1. **Summary** (the `summary` field): the question, the answer in two or three
   sentences, and how sure we are. A reader who stops here should know the result.
2. **Data**: where it came from (dataset, download, twin or full data), its size.
3. **What was done**: the method in plain words; show the code of the one or two
   cells that matter (`show: "all"`), not every cell.
4. **Results**: figures (`{"figure": "c0015"}`) and tables (`{"cell": ..., "show":
   "outputs"}`), each with a sentence saying what to see in it; findings and verdicts
   as `{"note": "n0007"}`.
5. **What did not work**: negative results, mistakes and what they taught. They
   are part of the result, not something to hide.
6. **How sure we are**: checks that passed or failed, twins versus full data, what a
   stricter test would need.
7. **Next steps**, if the study has any.

Short studies need fewer sections; a paper reproduction adds the table "paper vs ours".

## 3. Rules of thumb

- Write in the language of the student's request; explain terms once, for the audience
  (a professor wants the argument, a student the reasoning).
- Every number in your text should be visible in a cell or note the report shows. If it
  comes from elsewhere (a paper), say where. The warnings list the ones not found.
- `show: "outputs"` hides long code; `show: "code"` shows code without its output.
- A figure block takes the cell's first figure; `"c0015:2"` takes the second.
- Findings or verdicts you leave out go to `left_out` with a reason
  (`{"n0009": "superseded by n0012"}`); the appendix lists them either way.
- 15–40 blocks is typical. Do not paste the whole journal; the protocol notebook has it.

## 4. Draft, then publish

The HTML copy is for readers who skip code: it hides every code cell (the notebook
keeps them), so say in the text what a cell computed when the result depends on it.

`report(project, spec)` builds `reports/draft/` and returns warnings. They are advice:
fix the ones a reader would trip over, ignore the rest. `report(project, spec,
publish=true)` keeps it as `reports/NN-<title>/` (notebook, HTML without code, the
spec). Publishing again after more research makes a new numbered report; the old one
stays as it was. `report(project)` lists the published reports.

Do not start new analyses while writing. If the reader needs something no cell shows
(a summary table from saved results), one short presentation cell is fine; say so in
its `why`.
