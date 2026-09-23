#!/usr/bin/env bash
# Publish (or update) the shared, read-only sc-hub library. Run by the pilot owner
# on the login node; heavy work happens in a Slurm job.
#
#   bash scripts/publish_library.sh
#
# Layout (world-readable, never written by students):
#   $LIB/hub/<version>/        sc-hub source snapshot
#   $LIB/envs/<version>/       Python env with sc-hub installed (non-editable)
#   $LIB/envs/current          -> newest version (students pin the resolved path)
#   $LIB/python/               uv-managed interpreters used by the envs
#   $LIB/datasets/<name>/      data.h5ad + dataset.yaml
#   $LIB/models/celltypist/    CellTypist models
# Old env versions are kept: queued jobs of students still point at them.
set -euo pipefail
# Nothing published may be group- or world-writable: students share a Unix group.
umask 022

UV_VERSION="0.12.17"
UV_SHA256="fa82fd8dde8e8eefdecada6aa0889666556cfceb690d06e0c3bca49eb3070a63"
UV_ARCHIVE="uv-x86_64-unknown-linux-gnu.tar.gz"
PYTHON_VERSION="3.12"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="${SCHUB_LIBRARY_ROOT:-/l/users/$USER/sc-hub-library}"
PARTITION="${SCHUB_PARTITION:-ws-ia}"
TORCH_BACKEND="${SCHUB_TORCH_BACKEND:-cu128}"
ASSETS="${SCHUB_ASSETS:-pbmc3k kang2018 celltypist}"
VERSION="${SCHUB_LIBRARY_VERSION:-$(date +%Y%m%d-%H%M)}"
# Where the build runs. Default: a new GPU job. To reuse an allocation you already
# hold (for example a personal workstation job): SCHUB_SRUN_ARGS="--jobid=<id> --overlap"
# and SCHUB_GPU_CHECK=0 if that allocation has no GPU.
SRUN_ARGS="${SCHUB_SRUN_ARGS:---partition=$PARTITION --cpus-per-task=8 --mem=16G --gres=gpu:1 --time=01:00:00 --job-name=schub-library}"
GPU_CHECK="${SCHUB_GPU_CHECK:-1}"
SNAPSHOT=""
CREATED=0
PUBLISHED=0

log() { printf '[sc-hub library] %s\n' "$*"; }
die() { printf '[sc-hub library] ERROR: %s\n' "$*" >&2; exit 1; }

layout() {
  mkdir -p "$LIB"/{hub,envs,python,datasets,models,bin} "$LIB/.private"/{cache,uv,staging}
  chmod 755 "$LIB" "$LIB"/{hub,envs,python,datasets,models,bin}
  chmod 700 "$LIB/.private"
}

install_uv() {
  local uv="$LIB/bin/uv"
  if [ -x "$uv" ] && "$uv" --version | grep -q " $UV_VERSION"; then return; fi
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/$UV_ARCHIVE" "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_ARCHIVE"
  (cd "$tmp" && echo "$UV_SHA256  $UV_ARCHIVE" | sha256sum -c --quiet -) || die "uv checksum mismatch"
  tar -xzf "$tmp/$UV_ARCHIVE" -C "$tmp"
  install -m 755 "$tmp/uv-x86_64-unknown-linux-gnu/uv" "$uv"
  rm -rf "$tmp"
}

cleanup_on_failure() {
  local status=$?
  # Only what this run created, and only while it is not published: never an
  # existing version (a name collision must not delete a live environment).
  if [ "$status" -ne 0 ] && [ "$CREATED" -eq 1 ] && [ "$PUBLISHED" -eq 0 ]; then
    log "publish failed; removing the unpublished $VERSION"
    rm -rf "$LIB/envs/$VERSION" "$SNAPSHOT"
  fi
}

snapshot_source() {
  if [ -e "$LIB/hub/$VERSION" ] || [ -e "$LIB/envs/$VERSION" ] || [ -e "$LIB/.private/hub-$VERSION" ]; then
    die "version $VERSION already exists"
  fi
  # Staged privately; it appears under hub/ only when the version is published.
  SNAPSHOT="$LIB/.private/hub-$VERSION"
  mkdir -p "$SNAPSHOT"
  CREATED=1
  # Allowlist: only what the package and scripts need, never local caches or notes.
  (cd "$SRC_DIR" && rsync -a --exclude __pycache__ --exclude '*.egg-info' \
    pyproject.toml README.md src scripts templates "$SNAPSHOT/")
}

build_in_job() {
  local env="$LIB/envs/$VERSION" cmd
  cmd="set -euo pipefail
umask 022
export UV_CACHE_DIR='$LIB/.private/uv' UV_PYTHON_INSTALL_DIR='$LIB/python'
export UV_PYTHON_PREFERENCE=only-managed UV_TORCH_BACKEND='$TORCH_BACKEND'
# Students cannot write __pycache__ into the library, so compile everything now.
export UV_COMPILE_BYTECODE=1
'$LIB/bin/uv' venv --quiet --python $PYTHON_VERSION '$env'
'$LIB/bin/uv' pip install --quiet --python '$env/bin/python' '$SNAPSHOT[analysis]'
export SCHUB_ROOT='$LIB/.private' SCHUB_LIBRARY='$LIB' SCHUB_PYTHON='$env/bin/python'
[ '$GPU_CHECK' = 0 ] || '$env/bin/python' -m schub.cli gpu-check
'$env/bin/python' -m schub.cli fetch $ASSETS --into '$LIB/.private/staging'"
  log "building env $VERSION and fetching assets in Slurm (srun $SRUN_ARGS)..."
  # shellcheck disable=SC2086 # SRUN_ARGS is a list of flags
  srun $SRUN_ARGS bash -c "$cmd"
}

promote_staged_assets() {
  # New downloads land in .private/staging and appear to students only here,
  # after their permissions are fixed. Existing assets are never overwritten.
  local kind item
  for kind in datasets models; do
    [ -d "$LIB/.private/staging/$kind" ] || continue
    for item in "$LIB/.private/staging/$kind"/*; do
      [ -e "$item" ] || continue
      chmod -R a+rX,go-w "$item"
      if [ -e "$LIB/$kind/$(basename "$item")" ]; then
        log "keeping existing $kind/$(basename "$item")"
      else
        mv "$item" "$LIB/$kind/"
      fi
    done
  done
}

swap_link() {
  # Atomic replacement of a `current` symlink (rename is atomic, ln -sfn is not).
  ln -sfn "$2" "$1.new" && mv -T "$1.new" "$1"
}

publish() {
  mv "$SNAPSHOT" "$LIB/hub/$VERSION"
  # Everything students need must be readable and traversable; nothing writable.
  chmod -R a+rX,go-w "$LIB/hub/$VERSION" "$LIB/envs/$VERSION" "$LIB/python" "$LIB/datasets" "$LIB/models" "$LIB/bin"
  swap_link "$LIB/envs/current" "$VERSION"
  PUBLISHED=1
  swap_link "$LIB/hub/current" "$VERSION"
  cat >"$LIB/README.md" <<EOF
# sc-hub shared library (read-only)

Owner: $USER. Current environment: $VERSION. Students never write here; anything
missing is downloaded into their own \$SCHUB_ROOT/library-local instead.
EOF
  chmod 644 "$LIB/README.md"
  log "published $VERSION at $LIB"
}

main() {
  command -v sbatch >/dev/null || die "run this on the cluster login node"
  trap cleanup_on_failure EXIT
  layout
  install_uv
  snapshot_source
  build_in_job
  promote_staged_assets
  publish
}

main "$@"
