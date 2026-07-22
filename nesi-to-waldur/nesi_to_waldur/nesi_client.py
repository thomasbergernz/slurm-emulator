"""Standalone NeSI projects-database REST client.

Implements the same OAuth2 device-code flow as the `nesi-projects` Claude
Code plugin (public client `nesi-admin`, realm `admin`) and calls the same
read-only REST endpoints, but keeps its own token cache so it can be run
repeatedly outside a Claude Code session — this is what makes the migration
pipeline re-runnable against a fresh set of projects on its own schedule.

Endpoints (confirmed against plugin source, server/nesi-mcp.js):
  GET /api/projects/code/{code}
  GET /api/projects?filter=&status=&page=&pageSize=&sortName=&sortDirection=
  GET /api/projects/{id}/people
  GET /api/projects/{id}/compound-allocations
  GET /api/people?filter=&status=&page=&pageSize=
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import requests

from .nesi_config import NesiConfig, load_config

DEFAULT_TOKEN_FILE = Path(os.environ.get("NESI_TO_WALDUR_TOKEN_FILE", "~/.cache/nesi-to-waldur/nesi_token.json")).expanduser()
_EXPIRY_SKEW_SECONDS = 30


class NesiAuthError(RuntimeError):
    pass


class NesiClient:
    def __init__(self, config: NesiConfig | None = None, token_file: Path = DEFAULT_TOKEN_FILE):
        self.config = config or load_config()
        self.token_file = token_file

    # -- token cache -----------------------------------------------------

    def _read_cache(self) -> dict | None:
        if not self.token_file.exists():
            return None
        return json.loads(self.token_file.read_text())

    def _write_cache(self, token_response: dict) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        expires_at = time.time() + token_response.get("expires_in", 0)
        cache = {
            "access_token": token_response["access_token"],
            "refresh_token": token_response.get("refresh_token"),
            "expires_at": expires_at,
        }
        self.token_file.write_text(json.dumps(cache))
        self.token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600

    def _refresh(self, refresh_token: str) -> dict:
        resp = requests.post(
            self.config.token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_access_token(self) -> str:
        if self.config.access_token:
            # Manual-paste fallback, same convention as the nesi-projects
            # plugin's NESI_ACCESS_TOKEN: skips device flow entirely.
            return self.config.access_token
        cache = self._read_cache()
        if cache is None:
            raise NesiAuthError(
                "Not logged in. Run `python -m nesi_to_waldur.cli nesi-login` first."
            )
        if cache["expires_at"] - _EXPIRY_SKEW_SECONDS > time.time():
            return cache["access_token"]
        if not cache.get("refresh_token"):
            raise NesiAuthError(
                "Cached token expired and no refresh token available. Run "
                "`python -m nesi_to_waldur.cli nesi-login` again."
            )
        token_response = self._refresh(cache["refresh_token"])
        self._write_cache(token_response)
        return token_response["access_token"]

    # -- device-flow login -------------------------------------------------

    def login(self, poll_stream=sys.stdout) -> None:
        """Run the OAuth2 device-code flow interactively, printing the
        verification URL for the user to open, then poll until approved."""
        start = requests.post(
            self.config.device_url,
            data={"client_id": self.config.client_id},
            timeout=30,
        )
        start.raise_for_status()
        device = start.json()
        verification_uri = device.get("verification_uri_complete") or device["verification_uri"]
        print(f"Open this URL and approve: {verification_uri}", file=poll_stream, flush=True)

        # This realm's device-auth response has been observed returning
        # `interval: 600` (== expires_in) — a spec-compliant client honoring
        # that would poll exactly once, right at expiry, and always "time
        # out" even when approved promptly. Cap it; real `slow_down` errors
        # (handled below) still back off from there if the server means it.
        interval = min(device.get("interval", 5), 5)
        expires_in = device.get("expires_in", 600)
        deadline = time.time() + expires_in
        while time.time() < deadline:
            # Poll BEFORE sleeping, so an already-approved code (e.g. the
            # user clicked through before this loop even started) succeeds
            # on the very first attempt instead of waiting a full interval.
            resp = requests.post(
                self.config.token_url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.config.client_id,
                    "device_code": device["device_code"],
                },
                timeout=30,
            )
            body = resp.json()
            if resp.status_code == 200:
                self._write_cache(body)
                print("Login successful.", file=poll_stream, flush=True)
                return
            error = body.get("error")
            if error == "authorization_pending":
                time.sleep(interval)
                continue
            if error == "slow_down":
                interval += 5
                time.sleep(interval)
                continue
            raise NesiAuthError(f"Device flow failed: {body}")
        raise NesiAuthError("Device flow expired before approval.")

    # -- REST calls --------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        token = self.get_access_token()
        resp = requests.get(
            f"{self.config.api_base}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def get_project_by_code(self, code: str) -> dict:
        return self._get(f"/api/projects/code/{code}")

    def search_projects_page(
        self,
        filter: str = "",
        status: str = "Active",
        page: int = 0,
        page_size: int = 20,
        sort_name: str = "createdAt",
        sort_direction: str = "DESC",
    ) -> tuple[list[dict], int]:
        body = self._get(
            "/api/projects",
            params={
                "filter": filter,
                "status": status,
                "page": page,
                "pageSize": page_size,
                "sortName": sort_name,
                "sortDirection": sort_direction,
            },
        )
        # server-observed shape variance: some responses key the list as
        # `projects`, others as `rows` — handle both, as the plugin does.
        projects = body.get("projects") or body.get("rows") or []
        total = body.get("projectsCount", len(projects))
        return projects, total

    def search_projects(
        self,
        filter: str = "",
        status: str = "Active",
        page: int = 0,
        page_size: int = 20,
        sort_name: str = "createdAt",
        sort_direction: str = "DESC",
    ) -> list[dict]:
        projects, _total = self.search_projects_page(filter, status, page, page_size, sort_name, sort_direction)
        return projects

    def iter_all_projects(self, status: str = "Active", page_size: int = 100):
        """Page through every project matching `status`, yielding project
        dicts one at a time. Used for filters (like organization name) the
        NeSI search API doesn't support server-side — the `filter` query
        param only matches project name/code, not organization."""
        page = 0
        seen = 0
        while True:
            projects, total = self.search_projects_page(status=status, page=page, page_size=page_size)
            if not projects:
                return
            for p in projects:
                yield p
            seen += len(projects)
            if seen >= total:
                return
            page += 1

    def project_people(self, project_id: int) -> list[dict]:
        return self._get(f"/api/projects/{project_id}/people")

    def project_compound_allocations(self, project_id: int) -> list[dict]:
        return self._get(f"/api/projects/{project_id}/compound-allocations")

    def find_person(self, filter: str, status: str = "Active", page_size: int = 10) -> list[dict]:
        body = self._get(
            "/api/people",
            params={"filter": filter, "status": status, "page": 0, "pageSize": page_size},
        )
        return body.get("people", [])
