# Export a Seurat object for sc-hub: counts (MatrixMarket), cell metadata and
# embeddings as plain files. Runs with the library's R + Seurat tool.
#   Rscript seurat_export.R <object.rds> <out dir>
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2) stop("usage: seurat_export.R <object.rds> <out dir>")
rds <- args[[1]]
out <- args[[2]]
dir.create(out, recursive = TRUE, showWarnings = FALSE)
suppressPackageStartupMessages({
  library(Matrix)
  library(SeuratObject)
})

obj <- readRDS(rds)
if (!inherits(obj, "Seurat")) stop("not a Seurat object: ", paste(class(obj), collapse = ", "))
# Raw counts live in RNA. SCT "counts" are corrected and "integrated" holds ~2k genes,
# so those are used only when there is no RNA assay, and never as raw counts.
assay_name <- if ("RNA" %in% Assays(obj)) "RNA" else DefaultAssay(obj)
assay <- obj[[assay_name]]
if (inherits(assay, "Assay5")) assay <- JoinLayers(assay) # v5 may split layers per sample

layer_or_null <- function(layer) {
  m <- tryCatch(LayerData(assay, layer = layer), error = function(e) NULL)
  if (is.null(m) || nrow(m) == 0 || ncol(m) == 0) NULL else m
}
m <- if (assay_name %in% c("SCT", "integrated")) NULL else layer_or_null("counts")
kind <- "counts"
if (is.null(m)) {
  m <- layer_or_null("data")
  kind <- "data"
}
if (is.null(m)) stop("assay ", assay_name, " has neither a counts nor a data layer")
m <- as(m, "CsparseMatrix")

writeMM(m, file.path(out, "matrix.mtx"))
writeLines(rownames(m), file.path(out, "genes.txt"))
writeLines(colnames(m), file.path(out, "cells.txt"))
meta <- obj[[]]
write.csv(meta, file.path(out, "meta.csv"))
# Column types: CSV would turn factors like seurat_clusters ("0", "1") into numbers.
types <- vapply(meta, function(column) class(column)[[1]], character(1))
writeLines(paste(names(types), types, sep = "\t"), file.path(out, "meta_types.txt"))
for (reduction in Reductions(obj)) {
  write.csv(Embeddings(obj, reduction = reduction), file.path(out, paste0("emb_", reduction, ".csv")))
}
writeLines(
  c(paste("kind", kind), paste("assay", assay_name), paste("seuratobject", as.character(packageVersion("SeuratObject")))),
  file.path(out, "info.txt")
)
cat("exported", ncol(m), "cells x", nrow(m), "genes (", kind, ") from assay", assay_name, "\n")
