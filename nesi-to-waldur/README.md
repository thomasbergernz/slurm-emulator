# nesi-to-waldur

Repeatable pipeline that imports NeSI legacy projects-database entities
(organizations, projects, people, compound allocations) into Waldur. Full
entity mapping and rationale: see the migration plan this tool implements
(`/Users/tber027/.claude/plans/dazzling-zooming-cocoa.md` — copy into this
repo's `docs/` if you want it version-controlled alongside the tool).

Two independent stages, joined by a plain JSON manifest file:

```
extract  (NeSI  -> manifest.json)
load     (manifest.json -> Waldur)
```

Re-running the pipeline against a **different** batch of projects needs no
code changes — just a different `extract --filter`/`--project-codes`, and a
fresh manifest path passed to `load`. Crosswalk decisions (role mapping, OECD
FoS codes, facility/offering/plan identity) live in `crosswalks/*.yaml`, so
correcting or extending them is a data edit, not a code change either.

## Setup

```bash
cd migration
uv venv .venv
uv pip install -e .
uv pip install -e '.[dev]'   # only if you add test-only deps later
```

## 1. Authenticate against NeSI (one-time, repeat when the cached token expires)

```bash
uv run nesi-to-waldur nesi-login
```

Prints a verification URL — open it, approve, and the tool caches an access
token (`~/.cache/nesi-to-waldur/nesi_token.json`, mode 0600) and refreshes it
automatically on later runs until the refresh token itself expires.

Environment overrides (mirrors the `nesi-projects` Claude Code plugin's
environment scheme): `NESI_ENV` (`dev`/`test`/`prod`, default `dev`),
`NESI_API_URL`, `NESI_IAM_BASE`, `NESI_REALM` (default `admin`),
`NESI_CLIENT_ID` (default `nesi-admin`). These can also be set in
`~/.nesi-projects.env` (`KEY=value` lines, override path via `NESI_CONFIG`) —
the same config file the `nesi-projects` plugin itself reads.

**Manual token fallback**: if `NESI_ACCESS_TOKEN` is set (env var or in
`~/.nesi-projects.env`) — the same convention the `nesi-projects` plugin
uses — it's used directly as the bearer token, skipping the device flow
entirely. Handy if you already have a valid token cached from another tool,
or the device flow is being finicky (Keycloak's `admin` realm has been
observed returning a 600-second `interval` in its device-authorization
response, which earlier versions of this client mishandled — now fixed, but
this fallback avoids the whole dance if you already have a token).

## 2. Extract a manifest

```bash
# explicit codes
uv run nesi-to-waldur extract --project-codes uoa04227 uoa04198 --out manifests/uoa-batch-1.json

# a project name/code search filter (resolves to every matching Active project)
uv run nesi-to-waldur extract --filter uoa --out manifests/uoa-batch-1.json -v

# every project belonging to a given organization (substring match, case-insensitive) —
# pages through the whole project list since NeSI's search API only filters by name/code,
# not organization, so this is slower than --filter on a large instance
uv run nesi-to-waldur extract --organization landcare --out manifests/landcare.json -v

# ...restricted to projects that currently hold a non-expired allocation
# (compound_allocation.end_date is unset or in the future; a project whose only
# allocation has already ended is dropped, not just its expired allocation)
uv run nesi-to-waldur extract --organization landcare --current-allocations-only --out manifests/landcare-current.json -v
```

**Repeat for a new batch or a new scope**: run this again with a different
`--filter`/`--organization`/`--project-codes` (and `--current-allocations-only`
if only active allocations should be in scope) and a different `--out` path.
Nothing else changes.

Note on email resolution: the NeSI project-people endpoint doesn't return
email addresses directly, so `extract` does a best-effort name-based lookup
per person and logs a warning (visible with `-v`) when it can't uniquely
resolve one. Unresolved people are still included in the manifest (with
`email: ""`) — `load` skips permission-granting for them and says so in its
report, rather than guessing.

## 3. Load into Waldur

```bash
export WALDUR_API_URL=https://waldur.nesi-test.nznesi.io
export WALDUR_API_TOKEN=<staff-token>

# always dry-run first
uv run nesi-to-waldur load --manifest manifests/uoa-batch-1.json --dry-run --report-out reports/uoa-batch-1-dryrun.json

# then for real
uv run nesi-to-waldur load --manifest manifests/uoa-batch-1.json --report-out reports/uoa-batch-1.json
```

Every create/mutate call is logged to the report (`actions`), along with
anything skipped or warned about (`warnings`, `skipped`) — read the report
before trusting a run, especially the first one against a new instance.

**Idempotent**: re-running `load` against the *same* manifest is a no-op —
every entity is looked up by `backend_id` (customers, projects, resources) or
existing-permission check (project membership) before creating anything.

**How resources are created**: via the marketplace **order lifecycle**, not
`import_resource`. For each compound allocation the loader places an order
(`POST /api/marketplace-orders/`, limits inline), then drives it
`approve_by_provider` → `set_state_done` (the manual/offline path — the SLURM
offering has no connected backend to auto-complete it), then stamps the
resource's `backend_id` (`nesi-alloc-<id>`) so re-runs are idempotent.
`import_resource` is *not* used because it 500s when the offering has no
backend scope to import from. Order `limits` only accept **limit-based**
component types, so all crosswalked components must be limit-based on the
offering (see `crosswalks/resource_components.yaml`).

### `--user-strategy`

- `sso-only` (default, safe): only grants permissions to people who already
  have a matching Waldur account (matched by email). Everyone else is noted
  as skipped — their account and permissions will need a follow-up pass once
  they've logged in via SSO at least once.
- `pre-create`: creates stub Waldur user accounts for unmatched `Active`
  people. **Only use this if the target Waldur instance shares NeSI's
  Keycloak realm** and has `OIDC_MATCHMAKING_BY_EMAIL` enabled — otherwise
  this creates duplicate/orphaned accounts when the person eventually logs in
  via SSO. See the migration plan §4.2 for why this is the crux decision for
  the whole user-import strategy.

## One-time provider setup (not done by this tool)

Waldur Offerings and Plans are provider-admin objects, created once, ahead of
any import run — not per-batch. Before `load` can attach resources:

1. Have one Offering per NeSI facility (e.g. HPC3) on the target instance,
   type `Marketplace.Slurm`, owned by a service-provider customer, under a
   category. Add one component per crosswalked resource type
   (`cpu`, `storage_project`, `storage_scratch`, ...), **all limit-based**
   (see the resource-creation note above for why). Component management on an
   *Active* offering is **not** generic PUT/PATCH — those return 405
   unconditionally by design. Use the dedicated actions instead:
   `POST /api/marketplace-provider-offerings/{uuid}/create_offering_component/`
   (and `update_offering_component` / `switch_billing_mode` to change one).
2. Have one Plan per NeSI `allocation_class` (`Collaborator`, ...) on that
   Offering. A single default plan is fine if you aren't pricing per class.
3. Paste the resulting UUIDs into `crosswalks/resource_components.yaml`
   (`offering_uuid`, `plans.<class>`). Until filled in, `load` skips resource
   creation for that facility/class and says so in its report — it will not
   guess or fail silently.

Discovering the exact create payloads is easiest via DRF introspection against
the live instance: `OPTIONS` a collection endpoint and read `actions.POST` for
the required fields and choice enums, and `GET` an existing object of the same
kind for a concrete shape reference. That is how this tool's offering/plan/
component/order payloads were derived (Waldur's own docs describe these as
UI-only workflows).

## Known limitations (see the migration plan for full detail)

- **Users have no external-id field in Waldur** — matching relies on email,
  which NeSI doesn't expose on the project-people endpoint directly (see
  extract's email-resolution note above).
- **No project nesting in Waldur** — `parent_project_id` is not migrated.
- **`linux_username`/`linux_gid` have no Waldur field** — these must continue
  to live wherever the Site Agent/POSIX layer already resolves them; this
  tool does not attempt to carry them.
- **Marketplace API path prefix**: confirmed against the actual upstream
  `8.0.8` git tag as `marketplace-provider-offerings`/
  `marketplace-provider-resources` (no plain `marketplace-offerings` exists
  at this version). `waldur_client.py` still auto-probes and caches the
  result rather than hardcoding it, because the deployed image is a REANNZ
  fork (`nesinz/waldur-mastermind:8.0.8-staticfix4`) not independently
  checked — pin it via `WALDUR_MARKETPLACE_PREFIX` if that ever matters.
- **Historical usage (`allocations[].used`) is not backfilled** — Waldur's
  usage-backfill endpoint stamps the current date and can't backdate
  arbitrary past periods; this is an accepted gap, not a bug.
- **One compound allocation is assumed to belong to exactly one facility**,
  and its nested allocations are assumed to share one date range. Both held
  in the sample data used to build this tool; validate against your actual
  batch before trusting date-range and multi-facility edge cases.

## Tests

```bash
uv run python tests/test_load_smoke.py
```

A mocked-API smoke test covering the idempotent-create path, the
unknown-role-crosswalk warning path, and the missing-Offering-UUID skip
path — the three most likely ways a new/different manifest surprises this
pipeline. Not a full test suite; extend it as new edge cases show up in real
runs.
