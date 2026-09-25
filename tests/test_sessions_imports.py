from __future__ import annotations

import io
import json
import stat
import tarfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import scipy.io
import scipy.sparse as sp

from schub import fetch_reads
from schub.cellxgene_session import COOKIE, TokenGate
from schub.fastq import load_manifest
from schub.seurat import assemble, check_request, SeuratImportError
from schub.service import Hub, HubError
from schub.sessions import jupyter_command, session_dir, write_connection
from schub.slurm import Slurm

from .conftest import make_adata


@pytest.fixture
def hub(settings, cluster, ctx) -> Hub:
    return Hub(settings, Slurm(cluster))


def fake_tool(library: Path, name: str, executable: str) -> None:
    path = library / "tools" / name / "1.0" / executable
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    (library / "tools" / name / "current").symlink_to("1.0")


def running(cluster, hub, settings, job_id: str, port: int = 40123) -> None:
    cluster.jobs[job_id] = "RUNNING"
    write_connection(session_dir(settings, job_id), "ws-l1-004", port, "/lab?token=abc")


def test_jupyter_session_lifecycle(hub, settings, cluster):
    info = hub.start_session("jupyter", hours=2, gpu=True)
    script = cluster.scripts[info.session_id]
    assert info.state == "PENDING" and "waiting in the Slurm queue" in info.how_to_open
    assert "schub.sessions serve --kind jupyter" in script and "--gres=gpu:1" in script and "--time=02:00:00" in script
    with pytest.raises(HubError, match="already pending"):
        hub.start_session("jupyter")
    with pytest.raises(HubError, match="no running jupyter"):
        hub.session_line("jupyter")
    running(cluster, hub, settings, info.session_id)
    assert hub.session_line("jupyter") == "ws-l1-004 40123 /lab?token=abc"
    listed = hub.list_sessions()
    assert listed[0].node == "ws-l1-004" and "./schub-lab jupyter" in listed[0].how_to_open
    assert hub.stop_session(info.session_id).state not in {"RUNNING", "PENDING"}
    assert ["scancel", info.session_id] in cluster.calls


def test_session_inputs_are_checked(hub, settings, write_h5ad):
    with pytest.raises(HubError, match="hours"):
        hub.start_session("jupyter", hours=48)
    with pytest.raises(HubError, match="unknown session kind"):
        hub.start_session("rstudio")
    with pytest.raises(HubError, match="needs target"):
        hub.start_session("cellxgene")
    outside = write_h5ad(make_adata(), name="elsewhere.h5ad")
    with pytest.raises(HubError, match="inside the sc-hub areas"):
        hub.start_session("cellxgene", target=str(outside))
    inside = write_h5ad(make_adata(), directory=settings.root / "data")
    with pytest.raises(HubError, match="not installed"):
        hub.start_session("cellxgene", target=str(inside))
    fake_tool(settings.library, "cellxgene", "bin/python")
    info = hub.start_session("cellxgene", target="data/data.h5ad")
    assert info.target == str(inside.resolve()) and not info.gpu
    with pytest.raises(HubError, match="not inside your sc-hub workspace"):
        hub.start_session("jupyter", target="/etc")
    notebook = settings.root / "notebooks" / "run.ipynb"
    notebook.parent.mkdir()
    notebook.write_text("{}")
    assert hub.start_session("jupyter", target=str(notebook)).target == "notebooks/run.ipynb"


def test_connection_file_is_private_and_token_stays_off_the_command_line(tmp_path):
    write_connection(tmp_path, "node", 41000, "/lab?token=secret")
    mode = stat.S_IMODE((tmp_path / "connection.json").stat().st_mode)
    assert mode == 0o600 and json.loads((tmp_path / "connection.json").read_text())["port"] == 41000
    command = jupyter_command("/env/python", 41000, tmp_path, "notebooks/run.ipynb")
    assert "--ip=0.0.0.0" in command and command[-1] == "notebooks/run.ipynb"
    assert not any("token" in part.lower() for part in command)


def call_gate(gate: TokenGate, query: str = "", cookie: str = "") -> tuple[str, dict[str, str], bytes]:
    seen: dict[str, object] = {}

    def start_response(status, headers):
        seen["status"], seen["headers"] = status, dict(headers)

    environ = {"QUERY_STRING": query, "HTTP_COOKIE": cookie, "PATH_INFO": "/", "SCRIPT_NAME": ""}
    body = b"".join(gate(environ, start_response))
    return str(seen["status"]), seen["headers"], body  # type: ignore[return-value]


def test_token_gate_protects_cellxgene():
    app = lambda environ, start: (start("200 OK", []), [b"cellxgene"])[1]  # noqa: E731
    gate = TokenGate(app, "tok")
    assert call_gate(gate)[0].startswith("403")
    assert call_gate(gate, "token=wrong")[0].startswith("403")
    status, headers, _ = call_gate(gate, "token=tok")
    assert status.startswith("302") and f"{COOKIE}=tok" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
    assert call_gate(gate, cookie=f"{COOKIE}=tok")[2] == b"cellxgene"
    assert call_gate(gate, cookie=f"{COOKIE}=nope")[0].startswith("403")


def test_seurat_import_requests_are_checked(hub, settings, tmp_path):
    rds = settings.data_dir / "obj.rds"
    rds.write_bytes(b"x")
    with pytest.raises(SeuratImportError, match="invalid dataset name"):
        check_request(settings, str(rds), "../evil")
    outside = tmp_path / "outside.rds"
    outside.write_bytes(b"x")
    with pytest.raises(SeuratImportError, match="inside the sc-hub areas"):
        check_request(settings, str(outside), "ok")
    with pytest.raises(HubError, match="not installed"):
        hub.import_seurat("data/obj.rds", "pbmc_seurat")
    fake_tool(settings.library, "r-seurat", "bin/Rscript")
    job = hub.import_seurat("data/obj.rds", "pbmc_seurat")
    assert job.target.endswith("data/pbmc_seurat/data.h5ad")
    (settings.data_dir / "pbmc_seurat").mkdir()
    with pytest.raises(HubError, match="already exists"):
        hub.import_seurat("data/obj.rds", "pbmc_seurat")


def test_assemble_turns_seurat_export_into_anndata(tmp_path):
    counts = sp.random(5, 3, density=0.6, format="csc", random_state=0) * 10  # genes x cells, as R writes it
    scipy.io.mmwrite(tmp_path / "matrix.mtx", counts)
    (tmp_path / "genes.txt").write_text("CD3E\nMS4A1\nCD3E\nLYZ\nNKG7\n")
    (tmp_path / "cells.txt").write_text("c1\nc2\nc3\n")
    pd.DataFrame({"seurat_clusters": ["0", "1", "0"], "nCount_RNA": [1, 2, 3]}, index=["c3", "c1", "c2"]).to_csv(tmp_path / "meta.csv")
    pd.DataFrame({"UMAP_1": [0.1, 0.2, 0.3], "UMAP_2": [1, 2, 3]}, index=["c1", "c2", "c3"]).to_csv(tmp_path / "emb_umap.csv")
    (tmp_path / "info.txt").write_text("kind counts\nassay RNA\nseuratobject 5.4.0\n")
    (tmp_path / "meta_types.txt").write_text("seurat_clusters\tfactor\nnCount_RNA\tnumeric\n")
    adata = assemble(tmp_path)
    assert adata.shape == (3, 5) and adata.var_names.is_unique
    assert np.allclose(adata.X.toarray(), counts.T.toarray())
    assert adata.obs.loc["c1", "seurat_clusters"] == "1" and adata.obs["seurat_clusters"].dtype == "category"
    assert adata.obs["nCount_RNA"].dtype.kind in "if"
    assert adata.obsm["X_umap"].shape == (3, 2) and adata.uns["schub_import"]["kind"] == "counts"


def _tar(path: Path, files: dict[str, bytes], mode: str = "w") -> Path:
    with tarfile.open(path, mode) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_cached_download_reuses_a_verified_file(tmp_path, monkeypatch):
    payload = tmp_path / "payload"
    payload.write_bytes(b"index")
    sha = fetch_reads.sha256_file(payload)
    cached = tmp_path / "downloads" / f"{sha[:16]}-file.tar.xz"
    cached.parent.mkdir()
    cached.write_bytes(b"index")
    monkeypatch.setattr(fetch_reads, "_download", lambda *a: pytest.fail("must not download"))
    assert fetch_reads.cached_download("https://x/file.tar.xz", sha, tmp_path) == cached


def test_fetch_kallisto_index_places_files_and_catalog(tmp_path, monkeypatch):
    archive = _tar(tmp_path / "idx.tar.xz", {"human/index.idx": b"IDX", "human/t2g.txt": b"t\tg\tn\n"}, "w:xz")
    monkeypatch.setattr(fetch_reads, "cached_download", lambda url, sha, cache: archive)
    folder = fetch_reads.fetch_kallisto_index("human", tmp_path / "lib", tmp_path)
    assert (folder / "index.idx").read_bytes() == b"IDX" and (folder / "t2g.txt").is_file()
    assert "index_sha256" in (folder / "ref.yaml").read_text() and not archive.exists()


def test_fetch_pbmc_fastq_keeps_read_pairs_with_checksums(tmp_path, monkeypatch):
    names = {f"pbmc_1k_v3_fastqs/pbmc_1k_v3_S1_L00{lane}_{read}_001.fastq.gz": f"{lane}{read}".encode()
             for lane in (1, 2) for read in ("R1", "R2", "I1")}
    archive = _tar(tmp_path / "fastqs.tar", names)
    monkeypatch.setattr(fetch_reads, "cached_download", lambda url, sha, cache: archive)
    manifest_path = fetch_reads.fetch_pbmc1k_v3_fastq(tmp_path / "lib", tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest.technology == "10xv3" and len(manifest.samples[0].reads) == 2
    assert not list(manifest_path.parent.rglob("*_I1_*")) and len(manifest.files) == 4


def test_sessions_and_imports_do_not_count_as_pipelines(hub, settings, cluster):
    from .conftest import library_datasets

    hub.start_session("jupyter")
    fake_tool(settings.library, "r-seurat", "bin/Rscript")
    (settings.data_dir / "obj.rds").write_bytes(b"x")
    hub.import_seurat("data/obj.rds", "imported")
    make_adata().write_h5ad(library_datasets(settings) / "one.h5ad")
    for n in range(settings.limits.max_active_runs):
        plan = hub.plan(str(library_datasets(settings) / "one.h5ad"), [{"brick": "qc_filter", "params": {"min_genes": n}}])
        hub.submit(plan.plan_id)
    # The cap counts only the pipelines: the next plan waits in sc-hub's queue.
    with pytest.raises(HubError, match="already active"):  # at the cap: refused with the reason, nothing queued
        hub.submit(hub.plan(str(library_datasets(settings) / "one.h5ad"), [{"brick": "qc_filter", "params": {"min_genes": 99}}]).plan_id)


def test_the_token_never_reaches_tool_output(hub, settings, cluster):
    info = hub.start_session("jupyter")
    running(cluster, hub, settings, info.session_id)
    shown = json.dumps([s.model_dump() for s in hub.list_sessions()]) + hub.stop_session(info.session_id).model_dump_json()
    assert "token" not in shown and "abc" not in shown


def test_listing_sessions_costs_one_squeue_and_keeps_long_queued_ones(hub, settings, cluster):
    import os

    queued = hub.start_session("jupyter")
    old_meta = session_dir(settings, queued.session_id) / "session.json"
    os.utime(old_meta, (0, 0))  # queued for days: still listed, still blocks a second one
    ended = session_dir(settings, "999")
    (ended / "session.json").write_text(json.dumps({"kind": "cellxgene", "target": "", "gpu": False, "hours": 1, "started": "x"}))
    os.utime(ended / "session.json", (0, 0))  # ended long ago: not listed
    before = len(cluster.calls)
    listed = hub.list_sessions()
    queries = [c for c in cluster.calls[before:] if c[0] in {"sacct", "squeue", "scontrol"}]
    assert [s.session_id for s in listed] == [queued.session_id] and len(queries) == 1
    with pytest.raises(HubError, match="already pending"):
        hub.start_session("jupyter")


def test_listener_owners_from_proc_tables(tmp_path):
    from schub.sessions import listener_uids

    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    table = tmp_path / "tcp"
    table.write_text(header
                     + "   0: 00000000:9C40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  3525        0 1\n"
                     + "   1: 0100007F:9C40 0100007F:D431 01 00000000:00000000 00:00000000 00000000  4000        0 2\n")
    assert listener_uids(40000, (str(table),)) == {3525}  # only the LISTEN row counts
    assert listener_uids(40001, (str(table),)) == set()
    assert listener_uids(40000, (str(tmp_path / "missing"),)) is None


def test_wait_until_listening_sees_only_a_live_server(tmp_path):
    import subprocess
    import sys

    from schub.sessions import free_port, wait_until_listening

    port = free_port()
    server = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                              cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_until_listening(server, port, timeout_s=20)
    finally:
        server.terminate()
        server.wait()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert not wait_until_listening(dead, free_port(), timeout_s=3)


def test_jupyter_sessions_keep_the_token_out_of_kernels():
    from schub.jupyter_launch import take_token

    command = jupyter_command("/env/python", 41000, Path("/r"), "")
    assert command[1:3] == ["-m", "schub.jupyter_launch"]
    env = {"JUPYTER_TOKEN": "abc", "SCHUB_SESSION_TOKEN": "abc", "PATH": "/bin"}
    assert take_token(env) == "abc" and env == {"PATH": "/bin"}
    with pytest.raises(SystemExit):
        take_token({"PATH": "/bin"})


FAKE_RSCRIPT = '''#!{python}
import sys
from pathlib import Path
import scipy.io, scipy.sparse as sp
out = Path(sys.argv[3])  # Rscript seurat_export.R <rds> <folder>
out.mkdir(parents=True, exist_ok=True)
scipy.io.mmwrite(out / "matrix.mtx", sp.csc_matrix([[1.0, 0.0], [2.0, 3.0]]))  # genes x cells
(out / "genes.txt").write_text("CD3E\\nLYZ\\n")
(out / "cells.txt").write_text("c1\\nc2\\n")
(out / "meta.csv").write_text(",group\\nc1,a\\nc2,b\\n")
(out / "info.txt").write_text("kind counts\\nassay RNA\\n")
'''


def test_bench_import_seurat_builds_r_once_into_the_students_own_library(settings, tmp_path, monkeypatch, capsys):
    """No shared library (students build their own sc-hub): the first import builds R + Seurat, later ones reuse it."""
    import sys

    from schub import seurat
    from schub.bench import kernel_api

    build = tmp_path / "build_tools.sh"
    build.write_text(f'''set -e
echo "building r-seurat into $1"
mkdir -p "$1/tools/r-seurat/9.9/bin" && cat > "$1/tools/r-seurat/9.9/bin/Rscript" <<'R'
{FAKE_RSCRIPT.format(python=sys.executable)}
R
chmod 755 "$1/tools/r-seurat/9.9/bin/Rscript" && ln -sfn 9.9 "$1/tools/r-seurat/current"
''')
    monkeypatch.setattr(seurat, "BUILD_TOOLS", build)
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.delenv("SCHUB_LIBRARY", raising=False)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "obj.rds").write_bytes(b"x")
    h5ad = kernel_api.import_seurat(settings.data_dir / "obj.rds", "tcells")
    assert "building r-seurat" in capsys.readouterr().out
    assert h5ad == settings.data_dir / "tcells" / "data.h5ad" and h5ad.is_file()
    assert (settings.local_library / "tools" / "r-seurat" / "current").exists()
    build.write_text("echo 'must not run again'; exit 1\n")
    (settings.data_dir / "obj2.rds").write_bytes(b"x")
    assert kernel_api.import_seurat(settings.data_dir / "obj2.rds", "tcells2").is_file()
    with pytest.raises(RuntimeError, match="already exists"):
        kernel_api.import_seurat(settings.data_dir / "obj.rds", "tcells")


def test_a_failed_r_build_says_why(settings, tmp_path, monkeypatch):
    from schub import seurat
    from schub.bench import kernel_api

    build = tmp_path / "build_tools.sh"
    build.write_text("echo 'critical libmamba could not solve'; exit 1\n")
    monkeypatch.setattr(seurat, "BUILD_TOOLS", build)
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.delenv("SCHUB_LIBRARY", raising=False)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "obj.rds").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="could not solve"):
        kernel_api.import_seurat(settings.data_dir / "obj.rds", "tcells")
    monkeypatch.setattr(seurat, "BUILD_TOOLS", tmp_path / "missing.sh")
    with pytest.raises(RuntimeError, match="ask the library owner"):
        kernel_api.import_seurat(settings.data_dir / "obj.rds", "tcells")
