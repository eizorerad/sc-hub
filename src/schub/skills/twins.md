---
name: twins
description: Try code on a small stratified twin of a dataset first; how twins are built, found again and when not to trust them.
---
# Twins: small copies to try code on

```
small = bench.twin("/path/to/data.h5ad", stratify="perturbation", keep=["control"], fraction=0.05)
```

- A twin keeps a fraction of every group (at least `min_per_group` cells each), and
  the `keep` groups (controls) up to `max_keep`, with a fixed seed. Same arguments,
  same twin: it is built once and found again (`datasets(name)` lists the twins of
  a catalogued dataset).
- Use it for the loop of write, run, fix: it loads in seconds instead of minutes.
- Mark cells on it with `data_scope="twin"` and the final runs with `"full"`.
- Conclusions and numbers for the student come from full-data cells. A twin can hide
  rare groups or make small effects look noisy; if twin and full disagree, the full
  data wins and the difference is worth a note.
- Without `stratify` it is a plain random sample of cells.
