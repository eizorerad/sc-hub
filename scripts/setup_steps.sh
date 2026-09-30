#!/usr/bin/env bash
# The two parts of a student's private sc-hub environment, run in Slurm jobs by bootstrap_cluster.sh:
#
#   setup_steps.sh quick    the analysis tools (scanpy, CellTypist, pydeseq2, decoupler, Jupyter...) and the
#                           starter datasets: a few minutes; the student starts working after it
#   setup_steps.sh extras   in a background job: the deep-learning stack (torch with its CUDA libraries,
#                           scvi-tools: up to 6.5 GB to download) and a GPU check, after another try at a
#                           starter dataset still missing. $SCHUB_ROOT/setup-extras.json says where it
#                           stands (and setup-datasets-missing.txt which starter data is not there); the
#                           dashboard, cluster() and `schub doctor` read them.
#
# Both install the versions of one resolution (env-lock.txt), so adding the rest later changes nothing
# already installed: a kernel that runs meanwhile keeps working. One install into the environment at a
# time, whatever the node: a lock folder (mkdir is atomic on Lustre; a flock there holds on one node only).
#
# Environment (exported by bootstrap_cluster.sh): SCHUB_ROOT, SCHUB_SRC, SCHUB_PYTHON_VERSION,
# SCHUB_TORCH_BACKEND, SCHUB_ASSETS; SCHUB_GPU_CHECK=skip where no GPU can be seen (a login node).
set -Eeuo pipefail
: "${SCHUB_ROOT:?}" "${SCHUB_SRC:?}"
UV="$SCHUB_ROOT/bin/uv"
ENV="$SCHUB_ROOT/env"
LOCK="$SCHUB_ROOT/env-lock.txt"
STATUS="$SCHUB_ROOT/setup-extras.json"
MISSING="$SCHUB_ROOT/setup-datasets-missing.txt"
BUSY="$SCHUB_ROOT/.env-install.lock"
STALE_MIN=15  # a lock not refreshed for this long lost its holder (killed): the heartbeat refreshes it each minute
POLL_S="${SCHUB_LOCK_POLL_S:-20}"  # how often a waiting part looks at the lock again
TOKEN="${SLURM_JOB_ID:-none}.$(hostname).$$"  # this holder's mark in the lock
ASSETS="${SCHUB_ASSETS:-pbmc3k kang2018 celltypist}"
export UV_CACHE_DIR="$SCHUB_ROOT/.cache/uv" UV_PYTHON_INSTALL_DIR="$SCHUB_ROOT/.cache/python"
export UV_PYTHON_PREFERENCE=only-managed UV_TORCH_BACKEND="${SCHUB_TORCH_BACKEND:-cu128}"
BEAT=""
HELD=""
INSTALLED=""  # the lock's checksum as installed by the background part

say() { printf '[sc-hub] %s %s\n' "$(date +%H:%M)" "$*"; }
now() { date +%Y-%m-%dT%H:%M:%S%z; }  # (GNU and BSD date alike)
checksum() { sha256sum "$LOCK" | cut -c1-16; }

heartbeat() {  # a line a minute while a long step runs (the setup page shows the student it is alive)
  local what="$1" start
  start=$(date +%s)
  # (its sleep holds no output of ours: a stopped heartbeat leaves nothing that keeps the log open)
  ( set +eE
    trap - ERR TERM EXIT  # not the script's: a failed line here must not write a status or free the lock
    while sleep 60 </dev/null >/dev/null 2>&1; do
      kill -0 $$ 2>/dev/null || exit 0  # the script is gone ($$ is its PID in a subshell too)
      touch -c "$BUSY" 2>/dev/null  # the lock stays fresh while its holder works (-c: never creates it)
      say "still $what ($(( ($(date +%s) - start) / 60 )) min so far)"
    done ) &
  BEAT=$!
  disown "$BEAT" 2>/dev/null || true  # (stopping it later must not print "Terminated" on the setup page)
}

stop_heartbeat() { if [ -n "$BEAT" ]; then kill "$BEAT" 2>/dev/null || true; BEAT=""; fi; }

stale_lock() { [ -n "$(find "$BUSY" -maxdepth 0 -mmin +"$STALE_MIN" 2>/dev/null)" ]; }

take_lock() {  # waits while another part of the setup installs into the environment
  local waited=0 next_word=0
  until mkdir "$BUSY" 2>/dev/null; do
    if [ ! -e "$BUSY" ]; then  # released just now, or it cannot be made at all (quota, permissions)
      mkdir "$BUSY" && break
      return 1  # (mkdir said why)
    fi
    if stale_lock; then  # its holder was killed: one waiter takes it over, and only if it is still stale then
      if mkdir "$BUSY.takeover" 2>/dev/null; then
        if stale_lock; then rm -rf "$BUSY"; fi
        rmdir "$BUSY.takeover" 2>/dev/null || true
      elif [ -n "$(find "$BUSY.takeover" -maxdepth 0 -mmin +5 2>/dev/null)" ]; then
        rmdir "$BUSY.takeover" 2>/dev/null || true  # (a waiter killed in the middle of a takeover)
      fi
      continue
    fi
    if [ "$waited" -ge "$next_word" ]; then  # a line every few minutes: the setup page shows it is not stuck
      say "waiting for the other install into this environment to finish ($(sed -n 2p "$BUSY/owner" 2>/dev/null || true))"
      next_word=$(( next_word + 180 ))
    fi
    sleep "$POLL_S"
    waited=$(( waited + POLL_S ))
  done
  HELD=1
  printf '%s\njob %s on %s since %s\n' "$TOKEN" "${SLURM_JOB_ID:-none}" "$(hostname)" "$(date +%H:%M)" > "$BUSY/owner"
}

release_lock() {  # only our own (after a takeover, the lock at that path may be another holder's)
  if [ -n "$HELD" ] && [ "$(head -n 1 "$BUSY/owner" 2>/dev/null || true)" = "$TOKEN" ]; then rm -rf "$BUSY"; fi
  HELD=""
}

finish() { stop_heartbeat; release_lock; }
trap finish EXIT

fetch_datasets() {  # the starter datasets not there yet (a present one is skipped); notes what is still missing
  local missing=""
  say "the starter datasets: $ASSETS"
  # shellcheck disable=SC2086 # asset names, checked by bootstrap_cluster.sh
  "$SCHUB_ROOT/bin/schub" fetch -- $ASSETS || true
  # shellcheck disable=SC2086
  missing="$("$SCHUB_ROOT/bin/schub" assets $ASSETS --missing 2>/dev/null)" || missing="$ASSETS"
  if [ -n "${missing// /}" ]; then
    printf '%s\n' "$missing" > "$MISSING"
    say "could not download: $missing (running the setup's cluster step again downloads it)"
    return 1
  fi
  rm -f "$MISSING"
}

quick() {
  take_lock
  heartbeat "installing the analysis tools"
  # The versions already there are kept (uv prefers the pins of an existing output file): running the setup
  # again changes what a kernel uses only where sc-hub's requirements changed. An environment from before
  # this lock existed gives its own versions.
  if [ -f "$LOCK" ]; then
    cp "$LOCK" "$LOCK.new"
  elif [ -x "$ENV/bin/python" ]; then
    "$UV" pip freeze --quiet --python "$ENV/bin/python" --exclude-editable > "$LOCK.new" || rm -f "$LOCK.new"
  else
    rm -f "$LOCK.new"
  fi
  [ -x "$ENV/bin/python" ] || "$UV" venv --quiet --python "${SCHUB_PYTHON_VERSION:-3.12}" "$ENV"
  # the whole set, resolved once: this part and the background one install the same versions
  "$UV" pip compile --quiet --python "$ENV/bin/python" "$SCHUB_SRC/pyproject.toml" --extra analysis --extra ml \
    -o "$LOCK.new"
  mv "$LOCK.new" "$LOCK"
  "$UV" pip install --quiet --python "$ENV/bin/python" -c "$LOCK" -e "$SCHUB_SRC[analysis]"
  stop_heartbeat
  release_lock
  say "the analysis tools are installed"
  fetch_datasets || true  # (the student can start without one; the next run of the setup tries again)
}

status() {  # state, step, error: where the background part stands (written whole, then moved into place)
  local lock_sum="$INSTALLED"
  if [ -z "$lock_sum" ] && [ -f "$LOCK" ]; then lock_sum="$(checksum)"; fi
  printf '{"state": "%s", "step": "%s", "job": "%s", "updated": "%s", "started": "%s", "lock": "%s", "error": "%s"}\n' \
    "$1" "$2" "${SLURM_JOB_ID:-}" "$(now)" "${STARTED:-}" "$lock_sum" "${3:-}" > "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"
}

extras() {
  STARTED="$(now)"
  local log="$SCHUB_ROOT/logs/setup-extras-${SLURM_JOB_ID:-local}.log"
  STEP="the starter datasets"
  trap 'status failed "$STEP" "exit $? (log: $log)"' ERR
  trap 'status failed "$STEP" "stopped by Slurm: the time limit or a cancel (log: $log)"; exit 143' TERM
  status running "$STEP"
  fetch_datasets || true  # (another try; what is still missing is noted, the rest goes on)
  STEP="the deep-learning tools (torch, scvi-tools)"
  status running "$STEP"
  take_lock
  heartbeat "installing the deep-learning tools (up to 6.5 GB)"
  "$UV" pip install --quiet --python "$ENV/bin/python" -c "$LOCK" -e "$SCHUB_SRC[analysis,ml]"
  INSTALLED="$(checksum)"  # (while still holding the lock: the versions this install used)
  stop_heartbeat
  release_lock
  STEP="the GPU check"
  status running "$STEP"
  if [ "${SCHUB_GPU_CHECK:-}" = skip ]; then
    say "no GPU check here (no GPU on this machine): a GPU job checks it"
  elif ! "$SCHUB_ROOT/bin/schub" gpu-check; then  # the old one-part setup failed here too: GPU jobs would not work
    trap - ERR
    status failed "$STEP" "torch is installed but saw no GPU in this job (log: $log)"
    say "torch is installed, but it saw no GPU in this job"
    return 1
  fi
  trap - ERR
  status done ""
  say "the deep-learning tools are installed"
}

case "${1:-}" in
  quick) quick ;;
  extras) extras ;;
  *) echo "usage: setup_steps.sh quick|extras" >&2; exit 2 ;;
esac
