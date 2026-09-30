#!/usr/bin/env bash
# Click through every control of the dashboard in the installed Google Chrome and
# check what each one does, as students see it: served by the dashboard's own server
# (scripts/schub_view.py, its policies included). Without an argument it builds a demo
# dashboard from synthetic runs; with one it tests an existing view folder, e.g. the
# copy of the real dashboard: tests/e2e/run.sh ~/sc-hub-view
# E2E_FILE=1 opens the page as a file instead. Needs Node 18+ and Google Chrome;
# `npm install` here once. Screenshots and results land in tests/e2e/out/.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
[ -d "$here/node_modules/playwright-core" ] || (cd "$here" && npm install --no-audit --no-fund >/dev/null)
work="$(mktemp -d)"
server=""
trap '[ -n "$server" ] && kill "$server" 2>/dev/null; rm -rf "$work"' EXIT
python="${PYTHON:-$repo/.venv/bin/python}"
[ -x "$python" ] || python=python3
if [ $# -ge 1 ]; then
  view="$(cd "$1" && pwd)"
else
  "$python" "$here/demo_site.py" "$work" >/dev/null
  view="$work/view"
fi
if [ "${E2E_FILE:-}" = 1 ]; then
  node "$here/clickthrough.js" "file://$view/index.html" "$here/out"
  exit
fi
port="$("$python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
SCHUB_VIEW_DIR="$view" SCHUB_VIEW_PORT="$port" "$python" "$repo/scripts/schub_view.py" serve --no-refresh \
  --home "$work/home" </dev/null >"$work/server.log" 2>&1 &
server=$!
for _ in $(seq 50); do
  curl -fs --noproxy "*" "http://127.0.0.1:$port/_schub/status" >/dev/null 2>&1 && break
  sleep 0.2
done
node "$here/clickthrough.js" "http://127.0.0.1:$port/" "$here/out"
