"""`schub` command line: the same operations as the MCP tools, JSON output."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel

from .config import load_settings
from .cli_bench import add_bench_parsers, bench_handlers
from .cli_goal import add_goal_parsers, goal_handlers
from .cli_projects import add_parsers, handlers, read_arg
from .cli_tools import add_tool_parsers, tool_handlers
from .h5ad_profile import UnsupportedFile
from .library import celltypist_dirs, library_mode
from .projects import ProjectError
from .planner import DatasetOverrides, StepRequest
from .runs import RunError
from .service import Hub, HubError
from .slurm import SlurmError

DOCTOR_IMPORTS = {
    "anndata": "anndata",
    "scanpy": "scanpy",
    "scvi": "scvi-tools",
    "celltypist": "celltypist",
    "pydeseq2": "pydeseq2",
    "jupyterlab": "jupyterlab",
    "kb_python": "kb-python",
    "memento": "memento-de",
    "torch": "torch",
}


def _emit(value: Any) -> None:
    if isinstance(value, BaseModel):
        text = value.model_dump_json(indent=2)
    elif isinstance(value, list) and value and isinstance(value[0], BaseModel):
        text = json.dumps([v.model_dump(mode="json") for v in value], indent=2)
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, indent=2, default=str)
    sys.stdout.write(text + "\n")


def _load_steps(raw: str) -> list[StepRequest]:
    data = json.loads(read_arg(raw))
    return [StepRequest.model_validate(s) for s in data]


def _doctor(hub: Hub) -> dict[str, Any]:
    s = hub.settings
    report: dict[str, Any] = {
        "root": str(s.root),
        "library": library_mode(s),
        "python": str(s.python),
        "datasets": [f"{d.name} ({d.source})" for d in hub.datasets()],
        "celltypist_models": sorted({p.name for d in celltypist_dirs(s) if d.is_dir() for p in d.glob("*.pkl")}),
        "imports": {},
    }
    for module, distribution in DOCTOR_IMPORTS.items():
        try:
            importlib.import_module(module)
            report["imports"][module] = importlib.metadata.version(distribution)
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            report["imports"][module] = f"FAILED: {exc}"
    try:
        report["partitions"] = [p.name for p in hub.slurm.partitions()]
    except SlurmError as exc:
        report["partitions"] = f"FAILED: {exc}"
    report["torch_cuda_build"] = _torch_cuda_build()
    return report


def _torch_cuda_build() -> str:
    try:
        import torch

        return f"{torch.__version__} (CUDA {torch.version.cuda})"
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return f"FAILED: {exc}"


def _gpu_check() -> int:
    """Exit 0 only if torch can use a GPU here (run it inside a GPU job)."""
    import torch

    available = torch.cuda.is_available()
    _emit(
        {
            "torch": torch.__version__,
            "cuda_build": torch.version.cuda,
            "cuda_available": available,
            "device": torch.cuda.get_device_name(0) if available else None,
        }
    )
    return 0 if available else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schub", description="sc-hub pipelines on Slurm")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bricks", help="list bricks")
    sub.add_parser("brick", help="describe a brick").add_argument("name")
    sub.add_parser("datasets", help="list datasets")
    sub.add_parser("inspect", help="profile an .h5ad").add_argument("dataset")
    plan = sub.add_parser("plan", help="validate a pipeline")
    plan.add_argument("dataset")
    plan.add_argument("steps", help="JSON list of {brick, params} or a path to one")
    plan.add_argument("--species", choices=["human", "mouse"])
    plan.add_argument("--gene-ids", choices=["symbol", "ensembl"])
    submit = sub.add_parser("submit", help="submit a plan")
    submit.add_argument("plan_id")
    submit.add_argument("--force-new", action="store_true")
    sub.add_parser("status", help="run status").add_argument("run_id")
    sub.add_parser("runs", help="recent runs").add_argument("--limit", type=int, default=10)
    logs = sub.add_parser("logs", help="tail a step log")
    logs.add_argument("run_id")
    logs.add_argument("step", type=int)
    logs.add_argument("--lines", type=int, default=80)
    sub.add_parser("results", help="run results").add_argument("run_id")
    sub.add_parser("cancel", help="cancel a run").add_argument("run_id")
    sub.add_parser("notebook", help="write a Jupyter notebook for a run").add_argument("run_id")
    sub.add_parser("cluster", help="partition availability")
    sub.add_parser("doctor", help="check the installation")
    sub.add_parser("gpu-check", help="exit 0 if torch sees a GPU (run inside a GPU job)")
    fetch = sub.add_parser("fetch", help="download missing starter assets (run inside a job)")
    fetch.add_argument("names", nargs="+")
    fetch.add_argument("--force", action="store_true")
    fetch.add_argument("--into", type=Path, help="library root to write to (default: your local library)")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    add_parsers(sub)
    add_tool_parsers(sub)
    add_bench_parsers(sub)
    add_goal_parsers(sub)
    return parser


def _dispatch(hub: Hub, args: argparse.Namespace) -> Any:
    table = {
        "bricks": lambda: hub.bricks(),
        "brick": lambda: hub.brick(args.name),
        "datasets": lambda: hub.datasets(),
        "inspect": lambda: hub.inspect(args.dataset),
        "plan": lambda: hub.plan(
            args.dataset, _load_steps(args.steps), DatasetOverrides(species=args.species, gene_ids=args.gene_ids)
        ).summary(),
        "submit": lambda: hub.submit(args.plan_id, args.force_new),
        "status": lambda: hub.status(args.run_id),
        "runs": lambda: hub.runs(args.limit),
        "logs": lambda: hub.logs(args.run_id, args.step, args.lines),
        "results": lambda: hub.results(args.run_id),
        "cancel": lambda: hub.cancel(args.run_id),
        "notebook": lambda: hub.notebook(args.run_id),
        "cluster": lambda: hub.cluster(),
        "doctor": lambda: _doctor(hub),
        **handlers(hub, args),
        **tool_handlers(hub, args),
        **(bench_handlers(hub, args) if args.command.startswith("bench-") else {}),
        **(goal_handlers(hub, args) if args.command.startswith(("goal-", "engine-", "eval-")) else {}),
    }
    return table[args.command]()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    hub = Hub(load_settings())
    if args.command == "mcp":
        from .mcp_server import build_server

        build_server(hub).run()
        return 0
    if args.command == "gpu-check":
        return _gpu_check()
    if args.command == "ide-proxy":  # stdout is the ssh stream: nothing else may be printed
        from .bench.ide import proxy

        return proxy(hub.settings, hub.slurm)
    if args.command == "ide-setup":
        from .bench.ide import IdeError, setup

        try:
            _emit(setup(hub.settings, sys.stdin.read()))
        except IdeError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1
        return 0
    if args.command == "fetch":
        from .fetch import FetchError, fetch

        try:
            _emit(fetch(hub.settings, args.names, force=args.force, into=args.into))
        except (FetchError, ValueError) as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1
        return 0
    try:
        _emit(_dispatch(hub, args))
    except (HubError, RunError, SlurmError, UnsupportedFile, ProjectError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
