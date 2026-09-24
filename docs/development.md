# Developing sc-hub

Back to the [README](../README.md).

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest --cov=schub
tests/e2e/run.sh                         # the dashboard in Chrome: every control
tests/e2e/run.sh ~/sc-hub-workspace/view # ...or the mirror of the real one
```

Unit tests use a fake Slurm and tiny generated `.h5ad` files. The brick
implementations are exercised end to end on the cluster. The dashboard test
(Node 18+ and Google Chrome; it installs `playwright-core` on first run) clicks
every tab, menu, step, filter and copy button of a page opened as a file, as
students open it, and checks what each does: addresses, requests copied,
notebooks downloaded, the auto-refresh keeping the open step, a 375 px phone.
Results and screenshots go to `tests/e2e/out/`.
