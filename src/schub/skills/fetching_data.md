---
name: fetching_data
description: Download public data into a project (figshare, GEO, Zenodo, Hugging Face) so the journal records url, size and sha256.
---
# Getting data

```
path = bench.fetch("https://.../file.h5ad", md5="<if the source publishes one>")  # or sha256=
```

- It saves into the project's `data/` folder by default (`dest=` for another path),
  resumes an interrupted download, checks the HTTP status and the size, and records
  url, path, size and sha256 in the cell's journal entry. With `md5=` or `sha256=`
  (Zenodo and figshare publish md5) a mismatch deletes the file.
- With a checksum, a file is downloaded once per student into `$SCHUB_ROOT/cache/fetch/`
  and linked into each project that asks for it, under any name (projects fetching it at
  the same time wait for the first download). The linked file is read-only, because every
  project shares it: to change it (h5py "r+", `backed="r+"`), copy it into `work/` first.
  Delete a folder there to free its space.
- A busy server (HTTP 429 or 5xx) is retried; a 404 or 403 is not.
- Large files (tens of GB): fetch inside a `%%slurm` cell so the kernel stays free.
- First check `datasets()`: the starter data (PBMC 3k, Kang 2018, the CellTypist
  models) are already in your workspace, with checksums, and a shared library may add more.
- Where to find public data, with checked links and checksums:
  skills("dataset_sources"); perturbation screens: skills("perturbseq").
- GEO: use the https links (https://ftp.ncbi.nlm.nih.gov/geo/...); ftp:// is refused.
- Say where the data comes from (paper, accession, license) in the cell's `why`.
- Never download into another student's folder or the shared library.
- Data stays on the cluster: never copy matrices into the chat.
- Lab-internal, unpublished, patient or controlled-access data (dbGaP, EGA), or a
  course's own corpus: read it where it is; never copy it to another folder, cluster or
  service, and ask the student what its terms allow before sharing any result from it.
- Downloaded and registered inputs are never changed: derive into `work/`.
- A Seurat object (.rds): `bench.import_seurat("data/obj.rds", name)` (a path in the
  project) makes the dataset `name` in the sc-hub folder's `data/<name>/data.h5ad`, which
  `datasets()` lists. R + Seurat are built once (10-20 minutes) if no library has them;
  large objects in a `%%slurm --mem 32G` cell.
- Gene sets (MSigDB Hallmark, checksums): skills("gene_sets").
