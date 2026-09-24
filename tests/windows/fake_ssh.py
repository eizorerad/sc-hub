"""A fake `ssh` for the Windows mirror test: answers view-sum and view-pack from $FAKE_VIEW_SRC."""

import importlib.util
import os
import sys
from pathlib import Path

PACK = Path(__file__).resolve().parents[2] / "src" / "schub" / "dashboard" / "pack.py"
spec = importlib.util.spec_from_file_location("pack", PACK)  # standard library only: no sc-hub install needed
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)

args = sys.argv[1:]
command = " ".join(args[args.index("mbzuai-schub") + 1:])
source = Path(os.environ["FAKE_VIEW_SRC"])
if command == "schub/bin/schub view-sum":
    print(pack.heavy_sum(source))
elif command in ("schub/bin/schub view-pack full", "schub/bin/schub view-pack light"):
    pack.pack(source, command.rsplit(" ", 1)[1], sys.stdout.buffer)
    sys.stdout.buffer.flush()
else:
    sys.stderr.write(f"sc-hub: this key only opens sc-hub: {command}\n")
    sys.exit(126)
