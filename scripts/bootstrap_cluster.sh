#!/usr/bin/env bash
# sc-hub bootstrap for one user on the cluster login node. Safe to re-run.
#
#   bash scripts/bootstrap_cluster.sh
#
# Builds the user's own environment in two parts (scripts/setup_steps.sh): the analysis
# tools and the starter datasets (into $ROOT/library-local) in a few minutes, after which
# the student can work; then, in a background job, the deep-learning stack (torch,
# scvi-tools: up to 6.5 GB). With SCHUB_LIBRARY set to a shared library this user can
# read (scripts/publish_library.sh), it uses that instead: no private environment
# and no duplicate datasets.
#
# Overrides (env): SCHUB_ROOT, SCHUB_LIBRARY, SCHUB_PARTITION,
#   SCHUB_INSTALL_MODE=job|login, SCHUB_ASSETS, SCHUB_TORCH_BACKEND.
set -euo pipefail

UV_VERSION="0.12.17"
UV_SHA256="fa82fd8dde8e8eefdecada6aa0889666556cfceb690d06e0c3bca49eb3070a63"
UV_ARCHIVE="uv-x86_64-unknown-linux-gnu.tar.gz"
PYTHON_VERSION="3.12"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${SCHUB_ROOT:-/l/users/$USER/schub}"
LIBRARY="${SCHUB_LIBRARY:-}"  # none by default: everything in the user's own root
PARTITION="${SCHUB_PARTITION:-ws-ia}"
INSTALL_MODE="${SCHUB_INSTALL_MODE:-job}"
ASSETS="${SCHUB_ASSETS:-pbmc3k kang2018 celltypist}"
# Node driver 570 supports CUDA <= 12.8; newer default wheels (cu130) cannot see the GPU.
TORCH_BACKEND="${SCHUB_TORCH_BACKEND:-cu128}"
PYTHON=""
HEAVY_CMD=""

log() { printf '[sc-hub] %s\n' "$*"; }

asset_names() {  # words like pbmc3k, kallisto-human, pbmc1k_v3_fastq: never an option, never a path
  local word
  for word in $1; do
    [[ "$word" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || return 1
  done
}
die() { printf '[sc-hub] ERROR: %s\n' "$*" >&2; exit 1; }

make_layout() {
  mkdir -p "$ROOT"/{bin,data,projects,plans,runs,cache/steps,logs,notebooks,view,trash,.cache,sessions}
  chmod 700 "$ROOT/sessions"  # session tokens
  # Workspaces created before the library existed kept datasets in shared/.
  if [ -d "$ROOT/shared" ] && [ ! -e "$ROOT/library-local" ]; then
    mv "$ROOT/shared" "$ROOT/library-local"
    log "moved old shared/ to library-local/"
  fi
  mkdir -p "$ROOT/library-local"/{datasets,models}
  chmod 700 "$ROOT"
  if [ ! -e "$HOME/schub" ]; then ln -s "$ROOT" "$HOME/schub"; fi
}

shared_env() {
  # Prints the concrete env directory if the shared library env is usable and
  # nothing in it is group/world-writable (students share a Unix group).
  [ -n "$LIBRARY" ] || return 1
  local current="$LIBRARY/envs/current" env_dir writable
  [ -x "$current/bin/python" ] || return 1
  env_dir="$(readlink -f "$current")"
  # Everything students run or read from it: a folder another student could write to would let them
  # swap the environment, a tool or a reference under everyone.
  writable="$( { find "$LIBRARY" "$LIBRARY/envs" -maxdepth 0 -perm /022 -print
    find "$env_dir" "$LIBRARY/python" "$LIBRARY/datasets" "$LIBRARY/models" "$LIBRARY/tools" "$LIBRARY/refs" \
      "$LIBRARY/bin" ! -type l -perm /022 -print; } 2>/dev/null | head -n 1 || true)"
  if [ -n "$writable" ]; then
    log "refusing the shared library: $writable is writable by others"
    return 1
  fi
  "$env_dir/bin/python" -c "import schub" 2>/dev/null || return 1
  printf '%s\n' "$env_dir"
}

install_uv() {
  local uv="$ROOT/bin/uv"
  if [ -x "$uv" ] && "$uv" --version | grep -q " $UV_VERSION"; then return; fi
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/$UV_ARCHIVE" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_ARCHIVE"
  (cd "$tmp" && echo "$UV_SHA256  $UV_ARCHIVE" | sha256sum -c --quiet -) || die "uv checksum mismatch"
  tar -xzf "$tmp/$UV_ARCHIVE" -C "$tmp"
  install -m 755 "$tmp/uv-x86_64-unknown-linux-gnu/uv" "$uv"
  rm -rf "$tmp"
  log "installed uv $UV_VERSION"
}

run_heavy() {
  # Compute and downloads go to a compute node; the login node only submits.
  if [ "$INSTALL_MODE" = "job" ]; then
    srun --partition="$PARTITION" --cpus-per-task=8 --mem=16G "$@" --time=01:00:00 \
      --job-name=schub-bootstrap bash -c "$HEAVY_CMD"
  else
    bash -c "$HEAVY_CMD"
  fi
}

write_wrappers() {
  local exports="export SCHUB_ROOT='$ROOT' SCHUB_LIBRARY='$LIBRARY' SCHUB_PARTITION='$PARTITION' SCHUB_PYTHON='$PYTHON'"
  local guard="if [ ! -x \"\$SCHUB_PYTHON\" ]; then echo \"sc-hub: \$SCHUB_PYTHON is unavailable (library moved or access removed); re-run bootstrap to fall back to a private environment.\" >&2; exit 3; fi"
  cat >"$ROOT/bin/schub" <<EOF
#!/usr/bin/env bash
$exports
$guard
exec "\$SCHUB_PYTHON" -m schub.cli "\$@"
EOF
  cat >"$ROOT/bin/schub-mcp" <<EOF
#!/usr/bin/env bash
# stdio MCP server: stdout is the protocol channel, nothing else may write to it.
$exports
$guard
exec "\$SCHUB_PYTHON" -m schub.cli mcp
EOF
  # The forced command of the sc-hub SSH key (the installer limits the key to it).
  install -m 755 "$SRC_DIR/scripts/schub-gate" "$ROOT/bin/schub-gate"
  # Notebooks now open in a Jupyter session (schub session-start jupyter / ./schub-lab).
  rm -f "$ROOT/bin/schub-notebook"
  chmod 755 "$ROOT/bin/schub" "$ROOT/bin/schub-mcp"
}

build_private_env() {
  install_uv
  PYTHON="$ROOT/env/bin/python"
  write_wrappers
  export SCHUB_ROOT="$ROOT" SCHUB_SRC="$SRC_DIR" SCHUB_PYTHON_VERSION="$PYTHON_VERSION" \
    SCHUB_TORCH_BACKEND="$TORCH_BACKEND" SCHUB_ASSETS="$ASSETS"
  # (while the background part installs, this waits for it: setup_steps.sh keeps to one install at a time)
  HEAVY_CMD="bash '$SRC_DIR/scripts/setup_steps.sh' quick"
  log "installing the analysis tools (scanpy, CellTypist, DE, Jupyter) and the starter datasets: about 5 minutes"
  run_heavy
  start_extras
}

extras_field() {  # a field of setup-extras.json (written by setup_steps.sh; plain words, no quotes inside)
  [ -f "$ROOT/setup-extras.json" ] || return 0  # (not there yet: the first run)
  sed -n "s/.*\"$1\": \"\([^\"]*\)\".*/\1/p" "$ROOT/setup-extras.json" | head -n 1 || true
}

extras_status() {  # state job error: the background part's status before its job runs (setup_steps.sh writes it after)
  printf '{"state": "%s", "step": "", "job": "%s", "updated": "%s", "started": "", "lock": "", "error": "%s"}\n' \
    "$1" "$2" "$(date +%Y-%m-%dT%H:%M:%S%z)" "${3:-}" > "$ROOT/setup-extras.json.tmp"
  mv "$ROOT/setup-extras.json.tmp" "$ROOT/setup-extras.json"
}

extras_job_state() {  # prints the job's state while it is queued or running, nothing once it is gone;
  # fails when Slurm does not answer (then nobody can tell, and nothing is queued twice)
  local out
  [ -n "$1" ] || return 0
  if ! out="$(squeue -h -j "$1" -o %T 2>&1)"; then
    case "$out" in *"Invalid job id"*) return 0 ;; esac
    return 1
  fi
  printf '%s\n' "$out" | grep -E '^(PENDING|CONFIGURING|RUNNING|COMPLETING|SUSPENDED)$' | head -n 1 || true
}

start_extras() {
  # The deep-learning stack: a background job the student does not wait for. It starts ten minutes later,
  # so the setup's first Slurm jobs find a free job slot (2 on ws-ia; the workbench takes one).
  local state job lock_sum active prefix out
  state="$(extras_field state)"
  job="$(extras_field job)"
  lock_sum="$(sha256sum "$ROOT/env-lock.txt" 2>/dev/null | cut -c1-16 || true)"
  if [ "$state" = done ] && [ "$(extras_field lock)" = "$lock_sum" ]; then
    log "the deep-learning tools (torch, scvi-tools) are installed"
    return
  fi
  if [ "$state" = queued ] || [ "$state" = running ]; then
    if ! active="$(extras_job_state "$job")"; then
      log "Slurm did not answer about the background install (job $job), so it is not queued again; if it"
      log "does not finish, run this setup step again"
      return
    fi
    if [ -n "$active" ]; then
      log "the background install (job $job) is still on its way"
      return
    fi
  fi
  if [ "$INSTALL_MODE" != "job" ]; then
    # (right here, where no GPU can be checked; SLURM_JOB_ID would name an enclosing job as the background part's)
    env -u SLURM_JOB_ID SCHUB_GPU_CHECK=skip bash "$SRC_DIR/scripts/setup_steps.sh" extras 2>&1 \
      | tee "$ROOT/logs/setup-extras-local.log" \
      || log "the background part stopped (setup-extras.json says where); the rest of the setup goes on"
    return
  fi
  # this folder's job names (another sc-hub folder of the same account has its own: schub.config)
  prefix="$("$PYTHON" -c 'from schub.config import load_settings; print(load_settings().job_prefix)' \
    2>/dev/null || echo schub)"
  local err="$ROOT/logs/setup-extras-sbatch.err" why
  # (its warnings go to their own file: stdout is only the job id, "id" or "id;cluster")
  out="$(sbatch --parsable --partition="$PARTITION" --gres=gpu:1 --cpus-per-task=8 --mem=16G --time=02:00:00 \
    --begin=now+10minutes --job-name="$prefix-setup-extras" --output="$ROOT/logs/setup-extras-%j.log" \
    "$SRC_DIR/scripts/setup_steps.sh" extras 2>"$err")" || out=""
  job="$(printf '%s\n' "$out" | tail -n 1)"
  job="${job%%;*}"
  if ! [[ "$job" =~ ^[0-9]+$ ]]; then
    why="$(tr -d '"\\' < "$err" 2>/dev/null | tr '\n\r\t' '   ' | cut -c1-160 || true)"
    extras_status failed "" "could not queue it: ${why:-sbatch gave no job id}"
    log "could not queue the background install (${why:-sbatch gave no job id}). The rest of the setup"
    log "goes on; run this setup step again later for the deep-learning tools (torch, scvi-tools)"
    return
  fi
  extras_status queued "$job"
  local begin
  begin="$(date -d '+10 minutes' +%H:%M 2>/dev/null || date -v+10M +%H:%M)"
  log "you can start working now. In the background (job $job, from $begin): the deep-learning tools"
  log "(torch, scvi-tools: up to 6.5 GB to download, a few minutes to half an hour)"
}

fetch_missing() {
  local todo
  todo="$("$ROOT/bin/schub" assets $ASSETS --missing)" || die "could not check starter assets"
  asset_names "$todo" || die "unexpected asset list: $todo"
  if [ -z "${todo// /}" ]; then
    log "all starter assets are available"
    return
  fi
  log "not in the shared library, downloading into library-local: $todo"
  HEAVY_CMD="'$ROOT/bin/schub' fetch -- $todo"
  run_heavy
}

main() {
  command -v sbatch >/dev/null || die "run this on the cluster login node (sbatch not found)"
  asset_names "$ASSETS" || die "SCHUB_ASSETS may only contain asset names (letters, digits, _ and -)"
  make_layout
  local env_dir
  if env_dir="$(shared_env)"; then
    PYTHON="$env_dir/bin/python"
    log "using the shared library $LIBRARY (environment $(basename "$env_dir"))"
    write_wrappers
    fetch_missing
  else
    if [ -n "$LIBRARY" ]; then log "shared library $LIBRARY is not usable from this account"; fi
    build_private_env
  fi
  cp "$SRC_DIR/templates/AGENTS.cluster.md" "$ROOT/AGENTS.md"
  # Claude Code reads CLAUDE.md, not AGENTS.md; a student's own CLAUDE.md is left alone
  if [ ! -e "$ROOT/CLAUDE.md" ]; then printf '@AGENTS.md\n' > "$ROOT/CLAUDE.md"; fi
  "$ROOT/bin/schub" doctor
  "$ROOT/bin/schub" dashboard >/dev/null && log "dashboard written to $ROOT/view"
  log "done. MCP command for Codex:  ssh -T mbzuai-schub $ROOT/bin/schub-mcp"
}

main "$@"
