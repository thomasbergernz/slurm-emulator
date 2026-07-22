"""Shared `KEY=value` config-file parser (optional `export ` prefix, quoting,
`#`-comments ignored) — the format the `nesi-projects` plugin's
`~/.nesi-projects.env` uses, reused here for the Waldur side too so secrets
never need to be pasted into chat: put them in a local file instead.
"""

from __future__ import annotations

from pathlib import Path


def parse_env_file(path: Path, allowed_keys: tuple[str, ...]) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key in allowed_keys:
            values[key] = value
    return values
