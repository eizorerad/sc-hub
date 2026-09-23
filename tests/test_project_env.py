from __future__ import annotations

import pytest

from schub.project_env import EnvError, build_script, built, check_packages, kernel_ready
from schub.service import Hub, HubError
from schub.slurm import Slurm


@pytest.fixture
def hub(settings, cluster, ctx) -> Hub:
    (settings.library / "bin").mkdir()
    for tool in ("uv", "micromamba"):
        (settings.library / "bin" / tool).write_text("#!/bin/sh\n")
    hub = Hub(settings, Slurm(cluster))
    hub.create_project("crispr")
    return hub


def test_package_names_are_checked():
    check_packages(["harmonypy", "decoupler>=1.6,<2", "scib[rpy2]"], ["samtools=1.20"])
    for bad in (["git+https://evil/x"], ["../x"], ["a; rm -rf /"], ["x @ file:///etc"]):
        with pytest.raises(EnvError, match="not a package"):
            check_packages(bad, [])


def test_build_script_layers_on_the_shared_env(hub, settings):
    script = build_script(settings, "crispr/screen", ["harmonypy"], ["samtools"], "20260923-120000")
    assert "site.addsitedir" in script and "--no-deps" in script and "-c constraints.txt" in script
    assert "UV_TORCH_BACKEND=cu128" in script and "--channel bioconda" in script
    assert "--name schub-crispr.screen" in script and "'sc-hub: crispr/screen'" in script
    assert "^(-e |schub[ =@])" in script  # editable / local installs are no constraints
    # built in its own folder; `current` moves only after the .ok marker
    assert script.index('touch "$NEW/.ok"') < script.index('mv -T "$ROOT/current.new"')
    assert "micromamba" not in build_script(settings, "crispr", ["harmonypy"], [], "x")


def test_packages_build_on_what_is_installed_and_can_be_removed(hub, settings, cluster):
    job = hub.add_project_packages("crispr", pip=["harmonypy"])
    assert job.kernel == "sc-hub: crispr" and job.pip == ("harmonypy",) and "bash" in cluster.scripts[job.job_id]
    with pytest.raises(HubError, match="being built"):  # one build at a time: no request is lost
        hub.add_project_packages("crispr", pip=["decoupler"])
    cluster.jobs[job.job_id] = "FAILED"
    # the first build failed: a new request does not inherit its packages
    second = hub.add_project_packages("crispr", pip=["decoupler"])
    assert second.pip == ("decoupler",)
    cluster.jobs[second.job_id] = "COMPLETED"
    current = settings.root / "envs" / "crispr" / "20260923-120000"
    current.mkdir(parents=True)
    (current / "packages.json").write_text('{"pip": ["harmonypy", "decoupler"], "conda": ["samtools"], "built": "x"}')
    (settings.root / "envs" / "crispr" / "current").symlink_to("20260923-120000")
    assert built(settings, "crispr").conda == ("samtools",) and not kernel_ready(settings, "crispr")
    added = hub.add_project_packages("crispr", pip=["scib"])
    assert added.pip == ("harmonypy", "decoupler", "scib") and added.conda == ("samtools",)
    cluster.jobs[added.job_id] = "COMPLETED"
    dropped = hub.add_project_packages("crispr", pip=["decoupler"], remove=True)
    assert dropped.pip == ("harmonypy",)
    cluster.jobs[dropped.job_id] = "COMPLETED"
    gone = hub.add_project_packages("crispr", pip=["harmonypy", "decoupler"], conda=["samtools"], remove=True)
    assert gone.job_id == "" and not (settings.root / "envs" / "crispr").exists()  # moved aside, deleted in the background
    with pytest.raises(HubError, match="not a package"):
        hub.add_project_packages("crispr", pip=["x; y"])
