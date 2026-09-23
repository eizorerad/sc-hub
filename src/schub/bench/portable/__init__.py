"""Stdlib-only helpers a job can import in any Python environment (a paper's own venv too).

jobrun and the kernel put this folder on sys.path, so `from schub_ckpt import Run`
works the same in a kernel cell, a %%slurm job and a `--python` job.
"""
