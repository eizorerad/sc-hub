# Breadth evaluation results

`2026-09-24-claude.json`: `schub eval-score evals/requests.yaml` on the cluster test root,
Claude Code column only. Each request ran as a lab-agent goal (`schub eval-run --engines claude
--max-turns 3 --slice-minutes 40`, at most 30 GPU-minutes and 5 GB of downloads per request) under
the policy `mixed`, Claude first, `claude_weekly_ceiling 0.8`.

- 18 of 18 requests handed over as complete, in 19 turns, about $32 in total; Claude's seven-day
  window went from 71% to 74%.
- Expectations met everywhere except `k562-table-qc`, whose last perturbation check was a
  deliberate known-bad test by the agent (shuffled labels); `norman-combo-table`'s failing
  `min_cells` is a real property of the data at the agent's own threshold (one combination with
  54 cells).
- What the runs found in sc-hub, all fixed: an unallocated GPU visible to kernels on gpu nodes;
  concurrent downloads of the same file (now one download per checksum); a misleading
  `No qc_filter step` warning in chained `bench.run_brick` calls; the check name agents reach for
  (`file_exists`); a stopping workbench blocking a new one.

The Codex column is missing: on 2026-09-24 every Codex call answered "You hit your spend cap set
by the owner of your workspace". Rerun with `--engines codex` when the cap is raised, then
`schub eval-score` shows both columns.
