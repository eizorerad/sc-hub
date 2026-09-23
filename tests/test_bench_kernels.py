from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from schub.bench import executor
from schub.bench.executor import drain_ledger, execute
from schub.bench.kernels import ProjectKernel, kernel_env, runner_env
from schub.bench.outputs import OutputCollector


def test_env_keeps_allowlisted_names_and_drops_secrets() -> None:
    base = {"PATH": "/bin", "HOME": "/h", "SLURM_JOB_ID": "7", "SCHUB_ROOT": "/r", "LC_ALL": "C",
            "JUPYTER_TOKEN": "t", "SCHUB_SESSION_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k",
            "AWS_SECRET_ACCESS_KEY": "k", "GH_TOKEN": "t", "RANDOM_THING": "x", "MPLBACKEND": "Agg"}
    env = kernel_env(base, {"SCHUB_PROJECT": "demo"})
    assert env == {"PATH": "/bin", "HOME": "/h", "SLURM_JOB_ID": "7", "SCHUB_ROOT": "/r", "LC_ALL": "C",
                   "SCHUB_PROJECT": "demo"}
    with pytest.raises(ValueError):
        kernel_env(base, {"MY_TOKEN": "x"})


def test_proxy_passwords_are_removed_and_runner_env_is_marked() -> None:
    env = kernel_env({"https_proxy": "http://me:secret@proxy.mbzu.ae:3128", "http_proxy": "http://proxy:80"})
    assert env == {"https_proxy": "http://proxy.mbzu.ae:3128", "http_proxy": "http://proxy:80"}
    assert runner_env({"PATH": "/bin", "OPENAI_API_KEY": "k"}) == {"PATH": "/bin", "SCHUB_RUNNER_CLEAN": "1"}


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch):
    import os

    monkeypatch.setenv("JUPYTER_TOKEN", "must-not-leak")
    started = ProjectKernel("python3", tmp_path / "work", kernel_env(os.environ), epoch="local.1")
    started.start()
    yield started
    started.shutdown()


def run(kernel: ProjectKernel, tmp_path: Path, code: str, interrupt_after: float | None = None, kill: bool = False):
    collector = OutputCollector(tmp_path / "journal", "c0001", max_chars=2000)
    begin = time.monotonic()
    result = execute(kernel, code, collector, poll_s=0.1, on_progress=lambda: None,
                     should_interrupt=lambda: interrupt_after is not None and time.monotonic() - begin > interrupt_after,
                     should_kill=lambda: kill)
    return result, collector.snapshot()


@pytest.mark.kernel
def test_cells_share_state_and_run_in_the_project_folder(kernel: ProjectKernel, tmp_path: Path) -> None:
    result, outputs = run(kernel, tmp_path, "import os\nx = 41\nprint(os.getcwd())")
    assert result.status == "ok" and outputs[0].text.strip() == str(tmp_path / "work")
    result, outputs = run(kernel, tmp_path, "x + 1")
    assert outputs[0].kind == "result" and outputs[0].text == "42"


@pytest.mark.kernel
def test_no_token_in_the_kernel(kernel: ProjectKernel, tmp_path: Path) -> None:
    _, outputs = run(kernel, tmp_path, "import os\nprint(sorted(k for k in os.environ if 'TOKEN' in k))")
    assert outputs[0].text.strip() == "[]"


@pytest.mark.kernel
def test_errors_and_interrupts(kernel: ProjectKernel, tmp_path: Path) -> None:
    result, outputs = run(kernel, tmp_path, "{}['gene']")
    assert result.status == "error" and outputs[-1].ename == "KeyError"
    result, _ = run(kernel, tmp_path, "import time\ntime.sleep(60)", interrupt_after=1.0)
    assert result.status == "interrupted"
    result, outputs = run(kernel, tmp_path, "print('still alive')")
    assert result.status == "ok" and "still alive" in outputs[0].text


@pytest.mark.kernel
def test_dead_kernel_is_lost(kernel: ProjectKernel, tmp_path: Path) -> None:
    result, _ = run(kernel, tmp_path, "import os\nos._exit(1)")
    assert result.status == "lost"


@pytest.mark.kernel
def test_ledger_is_drained_even_after_a_failing_cell(kernel: ProjectKernel, tmp_path: Path) -> None:
    from schub.bench.ledger import parse_user_expression

    result, _ = run(kernel, tmp_path, "from schub.bench import ledger\nledger.record('job', job_id='812')\n1/0")
    assert result.status == "error"
    assert [e["job_id"] for e in parse_user_expression(drain_ledger(kernel))] == ["812"]
    assert parse_user_expression(drain_ledger(kernel)) == []


@pytest.mark.kernel
def test_a_cell_that_ignores_interrupts_is_killed_when_retiring(kernel: ProjectKernel, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(executor, "KILL_GRACE_S", 1.0)
    code = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(60)"
    started = time.monotonic()
    result, _ = run(kernel, tmp_path, code, interrupt_after=0.5, kill=True)
    assert result.status == "lost" and result.interrupted and time.monotonic() - started < 20
    assert not kernel.alive()


@pytest.mark.kernel
def test_kernel_uses_private_unix_sockets(kernel: ProjectKernel) -> None:
    import os
    import stat

    info = kernel._manager.get_connection_info()
    assert info["transport"] == "ipc"
    folder = kernel._sockets
    assert folder is not None and stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    kernel.shutdown()
    assert not folder.exists()
