"""Single entrypoint: `nesi-to-waldur <subcommand> ...`.

    nesi-to-waldur nesi-login                  # one-time OAuth2 device-flow login (repeat when the cached token expires)
    nesi-to-waldur extract --filter uoa --out manifests/uoa-batch.json
    nesi-to-waldur load --manifest manifests/uoa-batch.json --dry-run
    nesi-to-waldur load --manifest manifests/uoa-batch.json

Re-running `extract` with a different --filter/--project-codes, followed by
`load` against the new manifest file, is the whole repeat-the-process story —
no code changes needed for a new batch of projects.
"""

from __future__ import annotations

import sys

from . import extract, load
from .nesi_client import NesiClient
from .nesi_config import load_config


def _nesi_login(argv: list[str]) -> int:
    config = load_config()
    if config.access_token:
        print(
            "NESI_ACCESS_TOKEN is set (env or ~/.nesi-projects.env) — using it directly, "
            "no device-flow login needed."
        )
        return 0
    NesiClient(config=config).login()
    return 0


COMMANDS = {
    "nesi-login": _nesi_login,
    "extract": extract.main,
    "load": load.main,
}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: nesi-to-waldur {{{'|'.join(COMMANDS)}}} ...", file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
