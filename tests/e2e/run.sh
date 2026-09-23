#!/usr/bin/env bash
# Click through every control of the dashboard in the installed Google Chrome and
# check what each one does. Without an argument it builds a demo dashboard from
# synthetic runs; with one it tests an existing view folder, e.g. the mirror of the
# real dashboard: tests/e2e/run.sh ~/sc-hub-workspace/view
# Needs Node 18+ and Google Chrome; `npm install` here once. Screenshots and results
# land in tests/e2e/out/.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
[ -d "$here/node_modules/playwright-core" ] || (cd "$here" && npm install --no-audit --no-fund >/dev/null)
if [ $# -ge 1 ]; then
  view="$(cd "$1" && pwd)"
else
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  python="${PYTHON:-$repo/.venv/bin/python}"
  [ -x "$python" ] || python=python3
  "$python" "$here/demo_site.py" "$work" >/dev/null
  view="$work/view"
fi
node "$here/clickthrough.js" "file://$view/index.html" "$here/out"
