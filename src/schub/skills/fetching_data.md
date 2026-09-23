---
name: fetching_data
description: Download public data into a project (figshare, GEO, Zenodo, Hugging Face) so the journal records url, size and sha256.
---
# Getting data

```
path = bench.fetch("https://.../file.h5ad", sha256="<if the source publishes one>")
```

- It saves into the project's `data/` folder by default (`dest=` for another path),
  resumes an interrupted download, checks the HTTP status and the size, and records
  url, path, size and sha256 in the cell's journal entry. With `sha256=` a mismatch
  deletes the file.
- Large files (tens of GB): fetch inside a `%%slurm` cell so the kernel stays free.
- First check the shared library with `datasets()`: Kang 2018, PBMC 3k and PBMC 1k
  FASTQ are already there, with checksums.
- Say where the data comes from (paper, accession, license) in the cell's `why`.
- Never download into another student's folder or the shared library.
- Data stays on the cluster: never copy matrices into the chat.
