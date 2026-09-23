"""Start JupyterLab with the session token, then keep the token out of kernels.

    python -m schub.jupyter_launch <jupyter lab arguments>

JupyterLab reads the token from the JUPYTER_TOKEN variable, and every kernel it
starts inherits the server's environment, so a cell printing os.environ would show
the token. This launcher takes the token out of the environment first and hands it
to the server as configuration instead.
"""

from __future__ import annotations

import os
import sys
from typing import MutableMapping, Sequence

TOKEN_VARIABLES = ("JUPYTER_TOKEN", "SCHUB_SESSION_TOKEN")


def take_token(env: MutableMapping[str, str]) -> str:
    """The session token, removed from `env` together with sc-hub's own copy."""
    token = env.pop("JUPYTER_TOKEN", "")
    for name in TOKEN_VARIABLES:
        env.pop(name, None)
    if not token:
        raise SystemExit("schub.jupyter_launch: JUPYTER_TOKEN is not set")
    return token


def main(argv: Sequence[str] | None = None) -> None:
    token = take_token(os.environ)
    from jupyterlab.labapp import LabApp
    from traitlets.config import Config

    config = Config()
    config.IdentityProvider.token = token
    LabApp.launch_instance(argv=list(sys.argv[1:] if argv is None else argv), config=config)


if __name__ == "__main__":
    main()
