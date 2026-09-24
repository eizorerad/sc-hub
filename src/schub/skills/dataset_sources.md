---
name: dataset_sources
description: Where public single-cell and perturbation data live, with the links and checksums checked in 2026-09.
---
# Where the data are

Check `datasets()` first: your workspace already has PBMC 3k and Kang 2018 (a
shared library may add more). Everything below is fetched with `bench.fetch(url, md5=...)`
(skills("fetching_data")), and the cell's `why` names the paper and the license.

## Perturbation screens

- **scPerturb** (Zenodo record 10044268, CC-BY-4.0): harmonized h5ad, one obs
  column `perturbation` with `control`. Links and md5 of Replogle K562 essential,
  K562 genome-wide, RPE1 and Norman 2019 are in skills("perturbseq");
  the record's API lists the rest: `https://zenodo.org/api/records/10044268`.
- **Replogle 2022, the authors' files** (figshare article 20029387, CC BY 4.0):
  `https://ndownloader.figshare.com/files/<id>`; the API
  `https://api.figshare.com/v2/articles/20029387` gives names, sizes and md5.
  Raw single-cell K562 essential is 10.7 GB (file 35773219, md5
  `4f1122ce1c7f13299a68df6459a266d3`); the pseudo-bulk tables (one row per
  perturbation, 80 MB for K562 essential: file 35773070) are enough for many
  questions. The genome-wide single-cell files (66 GB) have no md5 on figshare:
  record the sha256 and say so.
- **Norman 2019** raw files: GEO GSE133344 (`GSE133344_filtered_matrix.mtx.gz`,
  barcodes, genes, cell identities) under
  `https://ftp.ncbi.nlm.nih.gov/geo/series/GSE133nnn/GSE133344/suppl/`.

## Everything else

- **GEO**: `https://ftp.ncbi.nlm.nih.gov/geo/series/GSE<first digits>nnn/GSE<id>/suppl/<file>`
  (https only). Read the folder listing in a cell (`urllib.request.urlopen(url)`)
  before choosing files.
- **Zenodo / figshare records**: their APIs return the files with checksums; fetch
  the API JSON in a cell and pass the checksum on.
- **Hugging Face**: `https://huggingface.co/<org>/<repo>/resolve/<commit>/<file>`
  (datasets: `https://huggingface.co/datasets/<org>/<repo>/resolve/...`), with the
  commit hash, not `main`, so the download can be repeated.

Before downloading tens of GB: say how much, where it goes (the project's `data/`,
3 TB quota per student) and whether a smaller file (pseudo-bulk, a filtered
matrix, a twin) answers the question.
