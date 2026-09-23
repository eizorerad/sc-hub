"""The code of a brick as a student reads it: the implementation module that runs
inside the Slurm job, made to stand in one notebook cell. Relative imports become
absolute and `run` is named after the brick, so several bricks share a notebook."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from .bricks import REGISTRY, get_brick
from .provenance import FileStamp, code_id, code_modules, file_stamp

RELATIVE_IMPORT = re.compile(r"^(?P<indent>[ \t]*)from (?P<dots>\.+)(?P<name>[\w.]*) import ", re.M)
RUN_DEF = re.compile(r"^def run\(", re.M)


def impl_module(brick: str) -> str:
    return get_brick(brick).impl.partition(":")[0]


def _absolute(module: str, source: str) -> str:
    package = module.rpartition(".")[0]

    def replace(match: re.Match[str]) -> str:
        target = importlib.util.resolve_name(match["dots"] + match["name"], package)
        return f"{match['indent']}from {target} import "

    return RELATIVE_IMPORT.sub(replace, source)


_SOURCES: dict[tuple[str, FileStamp], str] = {}  # (file, version): a long-running server sees updates


def brick_source(brick: str) -> str:
    """The brick's implementation, ready to paste into a notebook cell and run."""
    module = impl_module(brick)
    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        raise KeyError(f"no source for brick {brick!r}")
    path = Path(spec.origin)
    key = (str(path), file_stamp(path))
    if key not in _SOURCES:
        source = _absolute(module, path.read_text())
        renamed, count = RUN_DEF.subn(f"def {brick}(", source)
        if count != 1:
            raise ValueError(f"{module} must define exactly one top-level run()")
        _SOURCES[key] = renamed
    return _SOURCES[key]


def current_code_id(brick: str) -> str:
    """code_id of the installed brick (cached by file versions, so a long-running
    server sees an update)."""
    return code_id(get_brick(brick))


def code_changed(brick: str, recorded: str) -> bool:
    """True when a step ran with other code than the brick has now (after an update)."""
    return bool(recorded) and brick in REGISTRY and recorded != current_code_id(brick)
