#!/usr/bin/env bash
# Install 10x Genomics Cell Ranger and its references into the shared library,
# so every student's cellranger_count step can use them. Library owner, once.
#
# Cell Ranger is licensed by 10x Genomics. Open
#   https://www.10xgenomics.com/support/software/cell-ranger/downloads
# accept the license, copy the download link of the Linux tarball (it expires
# after a day) and, if you like, its md5 from the same page. Then on the login node:
#   bash scripts/install_cellranger.sh '<download link>' [human|mouse|both] [md5]
# Downloading and unpacking run in a Slurm job (the login node is not for that).
set -euo pipefail
umask 022

URL="${1:?usage: install_cellranger.sh '<download link from 10x>' [human|mouse|both] [md5]}"
WHICH="${2:-human}"
MD5="${3:-}"
LIB="${SCHUB_LIBRARY_ROOT:-/l/users/$USER/sc-hub-library}"
PARTITION="${SCHUB_PARTITION:-ws-ia}"

log() { printf '[sc-hub cellranger] %s\n' "$*"; }
# Only 10x's own download host and path; the signed query has no quotes or spaces.
if ! [[ "$URL" =~ ^https://cf\.10xgenomics\.com/releases/cell-exp/cellranger-[0-9][0-9.]*\.tar\.gz(\?[A-Za-z0-9=\&%_.~-]*)?$ ]]; then
  log "not a Cell Ranger download link from cf.10xgenomics.com/releases/cell-exp/"; exit 1
fi
[[ -z "$MD5" || "$MD5" =~ ^[0-9a-f]{32}$ ]] || { log "md5 must be 32 hex characters"; exit 1; }
case "$WHICH" in human | mouse) ORGS="$WHICH" ;; both) ORGS="human mouse" ;; *) log "second argument: human, mouse or both"; exit 1 ;; esac
command -v sbatch >/dev/null || { log "run this on the cluster login node"; exit 1; }

# The job receives every value as an argument, never pasted into its source.
# shellcheck disable=SC2016 # expanded by the job's own bash
JOB='set -euo pipefail
umask 022
url="$1"; lib="$2"; orgs="$3"; md5="$4"
work=$(mktemp -d "$lib/.private/cellranger.XXXX")
trap '\''rm -rf "$work"'\'' EXIT
mkdir -p "$lib/tools/cellranger" "$lib/refs/cellranger"
curl -fsSL -o "$work/cellranger.tar.gz" "$url"
if [ -n "$md5" ]; then echo "$md5  $work/cellranger.tar.gz" | md5sum -c --quiet -; fi
# awk reads the whole listing: `head` would close the pipe and fail the job under pipefail.
version=$(tar -tzf "$work/cellranger.tar.gz" | awk -F/ '\''NR == 1 { print $1 }'\'')
case "$version" in cellranger-[0-9]*) ;; *) echo "unexpected archive layout: $version"; exit 1 ;; esac
if [ ! -x "$lib/tools/cellranger/$version/cellranger" ]; then
  tar -xzf "$work/cellranger.tar.gz" -C "$work"
  "$work/$version/cellranger" --version
  mv "$work/$version" "$lib/tools/cellranger/"
fi
ln -sfn "$version" "$lib/tools/cellranger/current.new" && mv -T "$lib/tools/cellranger/current.new" "$lib/tools/cellranger/current"
for org in $orgs; do
  case "$org" in human) ref=refdata-gex-GRCh38-2024-A ;; mouse) ref=refdata-gex-GRCm39-2024-A ;; esac
  [ -f "$lib/refs/cellranger/$org/reference.json" ] && continue
  curl -fsSL -o "$work/$ref.tar.gz" "https://cf.10xgenomics.com/supp/cell-exp/$ref.tar.gz"
  tar -xzf "$work/$ref.tar.gz" -C "$work" && rm -f "$work/$ref.tar.gz"
  mv "$work/$ref" "$lib/refs/cellranger/$org"
done
chmod -R a+rX,go-w "$lib/tools/cellranger" "$lib/refs/cellranger"
echo "Cell Ranger $version ready for: $orgs"'

log "installing into $LIB in a Slurm job (~1 GB + ~11 GB per reference; up to an hour)"
srun --partition="$PARTITION" --cpus-per-task=4 --mem=16G --time=02:00:00 --job-name=schub-cellranger \
  bash -c "$JOB" install_cellranger "$URL" "$LIB" "$ORGS" "$MD5"
