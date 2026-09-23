"""Job entrypoint: `python -m schub.execute --step-dir DIR` inside a Slurm job."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .bricks import REGISTRY, BrickError, BrickSpec, StepIO, get_brick
from .provenance import code_id
from .stepfile import ERROR_FILE, STEP_FILE, SUCCESS, SUMMARY_FILE, StepFile

EXIT_CHECK_FAILED = 2
EXIT_CRASH = 1
# Lustre clients on this cluster sometimes fail a write with EFAULT ("Bad address")
# or EIO; the same step usually succeeds when simply run again.
TRANSIENT_ERRNOS = {5, 14}
ATTEMPTS = 3
RETRY_PAUSE_S = 30


def load_impl(dotted: str) -> Callable[..., dict[str, Any]]:
    module_name, _, attr = dotted.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def _write_error(step_dir: Path, kind: str, message: str, trace: str | None = None) -> None:
    payload = {"kind": kind, "message": message, "traceback": trace}
    (step_dir / ERROR_FILE).write_text(json.dumps(payload, indent=2))


def _prepare(step_dir: Path, registry: Mapping[str, BrickSpec]) -> tuple[BrickSpec, Any, StepIO]:
    step = StepFile.model_validate_json((step_dir / STEP_FILE).read_text())
    spec = get_brick(step.brick, registry)
    if step.version != spec.version:
        raise BrickError(f"planned with {spec.name} {step.version}, installed {spec.version}; plan again")
    if step.code_id and step.code_id != code_id(spec):
        raise BrickError(f"{spec.name} code changed after planning (sc-hub was updated); plan again")
    params = spec.params_model.model_validate(step.params)
    results_dir = Path(step.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    io = StepIO(
        input=Path(step.input),
        output=Path(step.output) if step.output else None,
        results_dir=results_dir,
        state_in=step.state_in,
        context=step.context,
    )
    return spec, params, io


def transient(exc: BaseException) -> bool:
    """A storage hiccup worth retrying (h5py only puts the errno in its message)."""
    if isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS:
        return True
    text = str(exc)
    return isinstance(exc, OSError) and any(f"errno = {n}," in text for n in TRANSIENT_ERRNOS)


def _run_with_retries(run: Callable[[], dict[str, Any]], pause_s: float = RETRY_PAUSE_S) -> dict[str, Any]:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return run()
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is a storage hiccup
            if attempt == ATTEMPTS or not transient(exc):
                raise
            print(f"[schub] storage error ({exc}); retrying the step ({attempt}/{ATTEMPTS - 1})", file=sys.stderr)
            time.sleep(pause_s * attempt)
    raise AssertionError("unreachable")


def run_step(step_dir: Path, registry: Mapping[str, BrickSpec] = REGISTRY) -> int:
    try:
        spec, params, io = _prepare(step_dir, registry)
    except BrickError as exc:
        _write_error(step_dir, "stale_plan", str(exc))
        return EXIT_CHECK_FAILED
    except Exception as exc:  # noqa: BLE001 - a broken step file must still leave a record
        _write_error(step_dir, "setup", f"{type(exc).__name__}: {exc}", traceback.format_exc())
        return EXIT_CRASH
    results_dir = io.results_dir
    started = datetime.now(timezone.utc)
    try:
        implementation = load_impl(spec.impl)
        summary = _run_with_retries(lambda: implementation(io, params))
    except BrickError as exc:
        _write_error(step_dir, "check_failed", str(exc))
        print(f"[schub] check failed: {exc}", file=sys.stderr)
        return EXIT_CHECK_FAILED
    except Exception as exc:  # noqa: BLE001 - every failure must leave a readable record
        _write_error(step_dir, "crash", f"{type(exc).__name__}: {exc}", traceback.format_exc())
        traceback.print_exc()
        return EXIT_CRASH
    finished = datetime.now(timezone.utc)
    timing = {
        "started": started.strftime("%Y-%m-%d %H:%M UTC"),
        "finished": finished.strftime("%Y-%m-%d %H:%M UTC"),
        "seconds": round((finished - started).total_seconds(), 1),
    }
    (results_dir / "timing.json").write_text(json.dumps(timing))
    (results_dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=2, default=str))
    (step_dir / SUCCESS).write_text(datetime.now(timezone.utc).isoformat())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schub.execute")
    parser.add_argument("--step-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_step(args.step_dir)


if __name__ == "__main__":
    raise SystemExit(main())
