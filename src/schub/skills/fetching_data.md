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
  and linked into each project that asks for it (projects fetching it at the same time
  wait for the first download). Delete a folder there to free its space.
- Large files (tens of GB): fetch inside a `%%slurm` cell so the kernel stays free.
- First check the shared library with `datasets()`: Kang 2018, PBMC 3k and PBMC 1k
  FASTQ are already there, with checksums.
- Where to find public data, with checked links and checksums:
  skills("dataset_sources"); perturbation screens: skills("perturbseq").
- GEO: use the https links (https://ftp.ncbi.nlm.nih.gov/geo/...); ftp:// is refused.
- Say where the data comes from (paper, accession, license) in the cell's `why`.
- Never download into another student's folder or the shared library.
- Data stays on the cluster: never copy matrices into the chat.
