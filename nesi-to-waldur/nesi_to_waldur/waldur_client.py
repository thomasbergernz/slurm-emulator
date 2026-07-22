"""Idempotent Waldur REST client for the load side of the migration.

Auth: `WALDUR_API_URL` + `WALDUR_API_TOKEN` — as process env vars, or (so a
staff token never has to be pasted into chat) `KEY=value` lines in a local
config file, default `~/.nesi-to-waldur-waldur.env` (override path via
`WALDUR_CONFIG`), same format/precedence as the NeSI side's
`~/.nesi-projects.env` (process env always wins over the file). Header
format is `Authorization: Token <token>` (NOT Bearer — this is Waldur's own
convention, distinct from NeSI's OIDC bearer tokens).

Path-prefix note: confirmed against the actual upstream `waldur-mastermind`
git tag `8.0.8` (not just `develop`) that this version already uses
`marketplace-provider-offerings`/`marketplace-provider-resources` — there is
no plain `marketplace-offerings` at this version at all. **Trap to avoid**:
a plain `marketplace-resources` prefix does exist at 8.0.8, but it's a
different viewset (`ConsumerResourceViewSet`) with a different action set
(`update_limits`, `switch_plan`, `terminate`, ...) — it does NOT have
`set_limits`/`set_backend_id`/`set_state_ok`. Hitting it by pattern-matching
on the shorter name would 404 those actions, not silently misbehave, but
it's an easy mistake. `_resolve_marketplace_prefix` sidesteps this by probing
the *offerings* side (where only the `-provider-` variant exists) and
deriving the resources prefix from whatever matched there, rather than
probing `-resources` variants independently.

The one remaining unknown is the REANNZ fork itself
(`nesinz/waldur-mastermind:8.0.8-staticfix4`) — "staticfix" suggests a
static-asset/branding patch, not an API change, but this isn't independently
verified against the fork's source. `WALDUR_MARKETPLACE_PREFIX` env var pins
the result explicitly if the probe ever needs overriding on this fork.

(The official `waldur-api-client` PyPI package, pinned to `==8.0.8`, wraps
these same endpoints with generated, typed bindings — e.g.
`waldur_api_client.api.marketplace_provider_resources.marketplace_provider_resources_set_limits`.
This module hand-rolls the same calls with plain `requests` to avoid adding
that dependency for what's currently ~10 endpoints; switch to it if this
client grows much further.)
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

from .env_file import parse_env_file

_MARKETPLACE_PREFIX_CANDIDATES = ["marketplace-provider-offerings", "marketplace-offerings"]
_CONFIG_KEYS = ("WALDUR_API_URL", "WALDUR_API_TOKEN", "WALDUR_MARKETPLACE_PREFIX")


def _config_value(key: str) -> str | None:
    if key in os.environ:
        return os.environ[key]
    config_file = Path(os.environ.get("WALDUR_CONFIG", "~/.nesi-to-waldur-waldur.env")).expanduser()
    return parse_env_file(config_file, _CONFIG_KEYS).get(key)


class WaldurApiError(RuntimeError):
    pass


class WaldurClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, dry_run: bool = False):
        resolved_base_url = base_url or _config_value("WALDUR_API_URL")
        resolved_token = token or _config_value("WALDUR_API_TOKEN")
        if not resolved_base_url or not resolved_token:
            raise RuntimeError(
                "WALDUR_API_URL / WALDUR_API_TOKEN not set. Set them as env vars, or put "
                "`KEY=value` lines in ~/.nesi-to-waldur-waldur.env (override path via WALDUR_CONFIG)."
            )
        self.base_url = resolved_base_url.rstrip("/")
        self.token = resolved_token
        self.dry_run = dry_run
        self._marketplace_prefix: str | None = _config_value("WALDUR_MARKETPLACE_PREFIX")
        self.actions: list[dict] = []  # audit log of every create/mutate call (or would-be call, in dry-run)
        self._dry_run_counter = 0

    # -- low level ---------------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Token {self.token}"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(f"{self.base_url}{path}", headers=self._headers(), params=params or {}, timeout=60)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()

    def _mutate(self, method: str, path: str, body: dict, description: str) -> dict:
        """POST/PUT/PATCH with dry-run support and an audit trail.

        Dry-run still returns a usable object (synthetic uuid/url) rather
        than a bare `{"dry_run": True}` marker: callers like `ensure_project`
        immediately dereference `["uuid"]`/`["url"]` on the result to keep
        going (grant permissions, import resources), and a dry run needs to
        walk that same path to report what *would* happen — not stop at the
        first create."""
        self.actions.append({"method": method, "path": path, "body": body, "description": description})
        if self.dry_run:
            self._dry_run_counter += 1
            synthetic_uuid = f"dry-run-{self._dry_run_counter}"
            return {"uuid": synthetic_uuid, "url": f"{self.base_url}{path}{synthetic_uuid}/", "dry_run": True}
        resp = requests.request(method, f"{self.base_url}{path}", headers=self._headers(), json=body, timeout=60)
        if not resp.ok:
            raise WaldurApiError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def _resolve_marketplace_prefix(self) -> str:
        if self._marketplace_prefix:
            return self._marketplace_prefix
        for candidate in _MARKETPLACE_PREFIX_CANDIDATES:
            resp = requests.get(f"{self.base_url}/api/{candidate}/", headers=self._headers(), timeout=30)
            if resp.status_code != 404:
                self._marketplace_prefix = candidate
                return candidate
        raise WaldurApiError(
            f"None of {_MARKETPLACE_PREFIX_CANDIDATES} resolved on {self.base_url} — "
            "the marketplace API path on this fork is unknown; check the live schema."
        )

    # -- customers (organizations) ------------------------------------------

    def get_customer_by_backend_id(self, backend_id: str) -> dict | None:
        results = self._get("/api/customers/", params={"backend_id": backend_id})
        return results[0] if results else None

    def ensure_customer(self, name: str, backend_id: str) -> dict:
        existing = self.get_customer_by_backend_id(backend_id)
        if existing:
            return existing
        return self._mutate(
            "POST", "/api/customers/", {"name": name, "backend_id": backend_id},
            f"create customer backend_id={backend_id}",
        )

    # -- projects ------------------------------------------------------------

    def get_project_by_backend_id(self, backend_id: str) -> dict | None:
        results = self._get("/api/projects/", params={"backend_id": backend_id})
        return results[0] if results else None

    def ensure_project(
        self, name: str, description: str, customer_url: str, backend_id: str,
        oecd_fos_2007_code: str | None = None,
    ) -> dict:
        existing = self.get_project_by_backend_id(backend_id)
        if existing:
            return existing
        body = {"name": name, "description": description, "customer": customer_url, "backend_id": backend_id}
        if oecd_fos_2007_code:
            body["oecd_fos_2007_code"] = oecd_fos_2007_code
        return self._mutate("POST", "/api/projects/", body, f"create project backend_id={backend_id}")

    def list_project_users(self, project_uuid: str) -> list[dict]:
        return self._get(f"/api/projects/{project_uuid}/list_users/") or []

    def add_project_user(self, project_uuid: str, user_uuid: str, role: str) -> None:
        current = self.list_project_users(project_uuid)
        if any(u.get("uuid") == user_uuid and u.get("role") == role for u in current):
            return
        self._mutate(
            "POST", f"/api/projects/{project_uuid}/add_user/", {"user": user_uuid, "role": role},
            f"add user={user_uuid} role={role} to project={project_uuid}",
        )

    # -- users -----------------------------------------------------------------

    def find_user_by_email(self, email: str) -> dict | None:
        """Best-effort — the `email` query filter on /api/users/ is not
        confirmed against this fork's version (only `?username=` is
        documented). Verify on first real run; if it returns nothing for a
        known-existing user, the filter name needs correcting here."""
        results = self._get("/api/users/", params={"email": email})
        return results[0] if results else None

    def create_user_stub(self, username: str, email: str, first_name: str, last_name: str) -> dict:
        """Staff-only. Only call this for the explicit pre-create strategy
        (migration plan §1.3) — requires confirming the target instance
        shares NeSI's Keycloak realm and has OIDC_MATCHMAKING_BY_EMAIL on,
        otherwise this creates duplicate/orphaned accounts at first SSO login."""
        return self._mutate(
            "POST", "/api/users/",
            {"username": username, "email": email, "first_name": first_name, "last_name": last_name},
            f"create user stub email={email}",
        )

    def grant_customer_permission(self, customer_url: str, user_uuid: str, role: str = "owner") -> None:
        self._mutate(
            "POST", "/api/customer-permissions/", {"customer": customer_url, "user": user_uuid, "role": role},
            f"grant customer permission user={user_uuid} role={role}",
        )

    # -- marketplace resources --------------------------------------------------

    def get_offering(self, offering_uuid: str) -> dict | None:
        prefix = self._resolve_marketplace_prefix()
        result = self._get(f"/api/{prefix}/{offering_uuid}/")
        return result or None

    def get_resource_by_backend_id(self, backend_id: str) -> dict | None:
        prefix = self._resolve_marketplace_prefix().replace("offerings", "resources")
        results = self._get(f"/api/{prefix}/", params={"backend_id": backend_id})
        return results[0] if results else None

    def _resource_prefix(self) -> str:
        return self._resolve_marketplace_prefix().replace("offerings", "resources")

    def create_resource_via_order(
        self, offering_uuid: str, project_uuid: str, plan_uuid: str,
        backend_id: str, name: str, limits: dict,
    ) -> dict:
        """Create a marketplace resource through the order lifecycle, the path
        for manual/SLURM offerings with no live backend to import from
        (import_resource 500s when the offering has no backend scope).

        Lifecycle proven against Waldur 8.0.8:
          POST /api/marketplace-orders/ (limits inline)  -> pending-provider
          POST .../{order}/approve_by_provider/          -> executing
          POST .../{order}/set_state_done/               -> done, resource OK
        then set_backend_id on the resource so re-runs are idempotent.

        URL forms are exact and non-obvious: offering = PUBLIC offering URL,
        plan = the NESTED public URL .../marketplace-public-offerings/{o}/plans/{p}/,
        project = plain project URL. Bare UUIDs or other URL forms 400.
        """
        existing = self.get_resource_by_backend_id(backend_id)
        if existing:
            return existing

        order = self._mutate(
            "POST", "/api/marketplace-orders/",
            {
                "offering": f"{self.base_url}/api/marketplace-public-offerings/{offering_uuid}/",
                "project": f"{self.base_url}/api/projects/{project_uuid}/",
                "plan": f"{self.base_url}/api/marketplace-public-offerings/{offering_uuid}/plans/{plan_uuid}/",
                "attributes": {"name": name},
                "limits": limits,
                "accepting_terms_of_service": True,
            },
            f"create order for resource backend_id={backend_id}",
        )
        if self.dry_run:
            return {"uuid": "dry-run", "dry_run": True}

        order_uuid = order["uuid"]
        resource_uuid = order.get("marketplace_resource_uuid")
        self._drive_order_to_done(order_uuid)

        if resource_uuid:
            self._mutate(
                "POST", f"/api/{self._resource_prefix()}/{resource_uuid}/set_backend_id/",
                {"backend_id": backend_id},
                f"set_backend_id resource={resource_uuid} backend_id={backend_id}",
            )
        return {"uuid": resource_uuid}

    def _drive_order_to_done(self, order_uuid: str) -> None:
        """Push an order through consumer/provider approval to Done, tolerant
        of whichever state it starts in (a staff/owner token often skips the
        consumer-approval step)."""
        order = self._get(f"/api/marketplace-orders/{order_uuid}/")
        state = order.get("state")
        if state == "pending-consumer":
            self._mutate("POST", f"/api/marketplace-orders/{order_uuid}/approve_by_consumer/", {}, f"approve_by_consumer {order_uuid}")
            state = self._get(f"/api/marketplace-orders/{order_uuid}/").get("state")
        if state == "pending-provider":
            self._mutate("POST", f"/api/marketplace-orders/{order_uuid}/approve_by_provider/", {}, f"approve_by_provider {order_uuid}")
            state = self._get(f"/api/marketplace-orders/{order_uuid}/").get("state")
        if state == "executing":
            self._mutate("POST", f"/api/marketplace-orders/{order_uuid}/set_state_done/", {}, f"set_state_done {order_uuid}")
            state = self._get(f"/api/marketplace-orders/{order_uuid}/").get("state")
        if state not in ("done",):
            raise WaldurApiError(f"order {order_uuid} did not reach 'done' (stuck at {state!r})")
