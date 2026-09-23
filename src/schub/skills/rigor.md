---
name: rigor
description: How to keep an analysis honest on the bench: why/expect, registration, decisions, baselines, numbers.
---
# Keeping the analysis honest

Each rule names the failure it prevents.

- **Every cell says why and what you expect.** `expect` is checked against what
  happened; a surprise is a finding, not something to smooth over.
- **Register before you look.** For a test that decides something, record a
  `registration` note first: the question, the smallest test that could reject the
  idea, and the rule that decides (pass, fail, what reverses it). Decisions made
  after seeing the result are labelled `descriptive`, not `registered`.
- **A decision names its evidence and its reversal.** `note(kind="decision",
  because=[cell refs], reverses_if="...")`. The journal refuses a decision without
  both. Write the reversal condition before the test that settles it.
- **Baselines before models.** A model's number means nothing without the trivial
  baseline (mean, no change, context mean) on the same split, in the journal.
- **Numbers only from outputs.** Quote a number with the cell ref it came from.
  Never estimate, round up or recall a number from memory or a paper as if it were
  measured here; paper numbers are `[UNVERIFIED]` until a cell reproduces them.
- **Test the check.** A check that never fails proves nothing: try it once on a
  known-bad input (a size check once passed a 404 error page).
- **Twin versus full.** Iterate on a small twin of a dataset; conclusions come from
  cells on the full data (`data_scope="full"`).
- **Record your own mistakes.** `note(kind="error", ...)` with what went wrong and
  what generalises. It is not a failure to report one; it is one to hide it.
- **Invalid is not negative.** A run that failed for a pipeline reason says nothing
  about the idea: mark it `invalid`, fix it, run again.
