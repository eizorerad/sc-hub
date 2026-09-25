#!/usr/bin/env bash
# Build the library's standalone tools, each in its own environment, once:
#   tools/cellxgene/<version>   cellxgene (pins numpy 2.0.1, so not in the main env)
#   tools/r-seurat/<version>    R + Seurat, to import .rds objects
#   bin/micromamba              alone (SCHUB_TOOLS=micromamba): for a project's conda packages
# Called by publish_library.sh inside its Slurm job:  build_tools.sh <library root>
# Environments are not relocatable, so they are built in place; `current` moves
# to a version only after it works.
set -euo pipefail
umask 022

LIB="${1:?usage: build_tools.sh <library root>}"
TOOLS="${SCHUB_TOOLS:-cellxgene r-seurat}"
CELLXGENE_VERSION="1.3.0"
SEURAT_VERSION="5.5.1"
MICROMAMBA_VERSION="2.9.0-0"
MICROMAMBA_SHA256="366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3"

log() { printf '[sc-hub tools] %s\n' "$*"; }
swap_link() { ln -sfn "$2" "$1.new" && mv -T "$1.new" "$1"; }

build_cellxgene() {
  local dir="$LIB/tools/cellxgene/$CELLXGENE_VERSION"
  if ! { [ -x "$dir/bin/python" ] && "$dir/bin/python" -c "import server.cli.launch" 2>/dev/null; }; then
    log "building cellxgene $CELLXGENE_VERSION"
    rm -rf "$dir" && mkdir -p "$(dirname "$dir")"
    "$LIB/bin/uv" venv --quiet --python 3.12 "$dir"
    "$LIB/bin/uv" pip install --quiet --python "$dir/bin/python" "cellxgene==$CELLXGENE_VERSION"
    "$dir/bin/python" -c "import server.cli.launch"
  fi
  swap_link "$LIB/tools/cellxgene/current" "$CELLXGENE_VERSION"
}

install_micromamba() {
  local bin="$LIB/bin/micromamba" tmp
  if [ -x "$bin" ] && "$bin" --version | grep -q "^${MICROMAMBA_VERSION%-*}"; then return; fi
  mkdir -p "$LIB/bin"  # a student's own library has none yet
  tmp="$(mktemp)"
  curl -fsSL -o "$tmp" "https://github.com/mamba-org/micromamba-releases/releases/download/$MICROMAMBA_VERSION/micromamba-linux-64"
  echo "$MICROMAMBA_SHA256  $tmp" | sha256sum -c --quiet - || { rm -f "$tmp"; log "micromamba checksum mismatch"; exit 1; }
  install -m 755 "$tmp" "$bin" && rm -f "$tmp"
}

build_seurat() {
  local dir="$LIB/tools/r-seurat/$SEURAT_VERSION"
  if ! { [ -x "$dir/bin/Rscript" ] && "$dir/bin/Rscript" -e 'suppressMessages(library(Seurat))' >/dev/null 2>&1; }; then
    install_micromamba
    log "building R + Seurat $SEURAT_VERSION (conda-forge; several minutes)"
    rm -rf "$dir" && mkdir -p "$(dirname "$dir")"
    MAMBA_ROOT_PREFIX="$LIB/.private/mamba" "$LIB/bin/micromamba" create --yes --quiet --prefix "$dir" \
      --override-channels --channel conda-forge "r-seurat=$SEURAT_VERSION" r-matrix >/dev/null
    "$dir/bin/Rscript" -e 'suppressMessages(library(Seurat)); cat("Seurat", as.character(packageVersion("Seurat")), "\n")'
    MAMBA_ROOT_PREFIX="$LIB/.private/mamba" "$LIB/bin/micromamba" list --prefix "$dir" --explicit >"$dir/conda-explicit.txt"
  fi
  swap_link "$LIB/tools/r-seurat/current" "$SEURAT_VERSION"
}

mkdir -p "$LIB/tools"
for tool in $TOOLS; do
  case "$tool" in
    cellxgene) build_cellxgene ;;
    r-seurat) build_seurat ;;
    micromamba) install_micromamba ;;  # alone: for a project's conda packages
    *) log "unknown tool $tool"; exit 1 ;;
  esac
done
log "tools ready: $TOOLS"
