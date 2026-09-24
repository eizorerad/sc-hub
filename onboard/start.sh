#!/usr/bin/env sh
# Start the sc-hub setup page (macOS, Linux). Needs Python 3.9+; without one, uv fetches it.
#   sh onboard/start.sh            (from a checkout of sc-hub)
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    cd "$HERE" && exec "$candidate" -m sc_hub_onboard "$@"
  fi
done
if ! command -v uv >/dev/null 2>&1; then
  echo "[sc-hub] Python 3.9+ not found: installing uv (https://astral.sh/uv) to run the setup"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
fi
cd "$HERE" && exec uv run --no-project --python 3.12 python -m sc_hub_onboard "$@"
