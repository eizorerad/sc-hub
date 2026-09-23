#!/usr/bin/env bash
# Build the installers and publish them where students download them from.
#
#   bash scripts/release.sh            publish to the shared library on the cluster
#   bash scripts/release.sh --gist     also update the secret gist
#
# The cluster copy lives in the pilot owner's world-readable library, so only
# people with a cluster account can fetch it, over the same SSH they already use.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER_ALIAS="${SCHUB_OWNER_ALIAS:-mbzuai}"
LIBRARY="${SCHUB_LIBRARY_ROOT:-/l/users/leonid.klarov/sc-hub-library}"
HOST="${SCHUB_HOST:-login-student-lab.mbzu.ae}"
GIST_FILE="$ROOT/installer/GIST_ID"

python3 "$ROOT/scripts/build_installer.py"

ssh -o BatchMode=yes "$OWNER_ALIAS" "umask 022; mkdir -p '$LIBRARY/install'"
scp -q "$ROOT/dist/install-sc-hub.sh" "$ROOT/dist/install-sc-hub.ps1" "$OWNER_ALIAS:$LIBRARY/install/"
ssh -o BatchMode=yes "$OWNER_ALIAS" "chmod 755 '$LIBRARY/install'; chmod 644 '$LIBRARY/install/'install-sc-hub.*"

if [ "${1:-}" = "--gist" ]; then
  if [ -s "$GIST_FILE" ]; then
    id="$(tr -d '[:space:]' <"$GIST_FILE")"
    gh gist edit "$id" -f install-sc-hub.sh "$ROOT/dist/install-sc-hub.sh"
    gh gist edit "$id" -f install-sc-hub.ps1 "$ROOT/dist/install-sc-hub.ps1"
  else
    url="$(gh gist create --desc "sc-hub installer" "$ROOT/dist/install-sc-hub.sh" "$ROOT/dist/install-sc-hub.ps1")"
    printf '%s\n' "${url##*/}" >"$GIST_FILE"
  fi
fi

cat <<EOF
Published to $LIBRARY/install. Students run one command (their cluster password is asked once or twice):
  macOS / Linux:  ssh LOGIN@$HOST cat $LIBRARY/install/install-sc-hub.sh | bash -s -- LOGIN
  Windows:        ssh LOGIN@$HOST cat $LIBRARY/install/install-sc-hub.ps1 | Out-String | iex
EOF
