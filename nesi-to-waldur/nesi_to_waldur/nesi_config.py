"""NeSI API + Keycloak endpoint resolution.

Mirrors the environment defaults AND the config-file convention used by the
`nesi-projects` Claude Code plugin (github.com/nesi1/nesi-collab/claude/
nesi-projects-plugin, server/nesi-mcp.js) so this standalone tool talks to
the same environments and can reuse the same manual-token fallback, but it
keeps its own token cache for device-flow logins — it does not read or
depend on the plugin's internal device-flow token file format.

Config file: `~/.nesi-projects.env` by default (override path via
`NESI_CONFIG`), `KEY=value` lines, optional `export ` prefix and quoting,
`#`-comments ignored — same format the plugin reads. A process environment
variable of the same name always takes precedence over the file, matching
the plugin's behavior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .env_file import parse_env_file

_DEFAULTS = {
    "dev": {
        "api": "https://api.dev.flexi.nesi.org.nz",
        "iam": "https://iam.dev.nesi.org.nz",
    },
    "test": {
        "api": "https://api.test.nesi.org.nz",
        "iam": "https://iam.test.nesi.org.nz",
    },
    "prod": {
        "api": "https://api.nesi.org.nz",
        "iam": "https://iam.nesi.org.nz",
    },
}

_CONFIG_KEYS = (
    "NESI_ENV", "NESI_API_URL", "NESI_IAM_BASE", "NESI_REALM", "NESI_CLIENT_ID",
    "NESI_ACCESS_TOKEN",
)


@dataclass(frozen=True)
class NesiConfig:
    api_base: str
    iam_base: str
    realm: str
    client_id: str
    access_token: str | None = None  # manual-paste fallback; skips device flow entirely when set

    @property
    def device_url(self) -> str:
        return f"{self.iam_base}/realms/{self.realm}/protocol/openid-connect/auth/device"

    @property
    def token_url(self) -> str:
        return f"{self.iam_base}/realms/{self.realm}/protocol/openid-connect/token"


def load_config() -> NesiConfig:
    config_file = Path(os.environ.get("NESI_CONFIG", "~/.nesi-projects.env")).expanduser()
    file_values = parse_env_file(config_file, _CONFIG_KEYS)

    def get(key: str, default: str | None = None) -> str | None:
        # process env always wins over the config file, matching the plugin
        return os.environ.get(key) or file_values.get(key) or default

    env = get("NESI_ENV", "dev")
    defaults = _DEFAULTS.get(env, _DEFAULTS["dev"])
    api_base = get("NESI_API_URL", defaults["api"]).rstrip("/")
    iam_base = get("NESI_IAM_BASE", defaults["iam"]).rstrip("/")
    realm = get("NESI_REALM", "admin")
    client_id = get("NESI_CLIENT_ID", "nesi-admin")
    access_token = get("NESI_ACCESS_TOKEN") or None  # empty string in the file counts as unset
    return NesiConfig(
        api_base=api_base, iam_base=iam_base, realm=realm, client_id=client_id,
        access_token=access_token,
    )
