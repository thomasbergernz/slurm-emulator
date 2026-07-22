"""Build a Manifest from the NeSI projects database for a given set of
project codes (or a search filter / organization / allocation-currency
selection that resolves to a set of codes).

Re-running this against a different `--project-codes` / `--filter` /
`--organization` argument is the whole point: it produces a fresh,
independent manifest file that `load.py` can then apply to Waldur, with no
code changes required to handle a new batch or a new scope of projects.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from .manifest import (
    AllocationComponent,
    CompoundAllocation,
    Manifest,
    Organization,
    Person,
    Project,
    ProjectMember,
)
from .nesi_client import NesiClient

log = logging.getLogger("nesi_to_waldur.extract")


def _science_props(project_json: dict) -> tuple[str | None, str | None]:
    default_props = (project_json.get("properties") or {}).get("default") or {}
    return default_props.get("science_domain"), default_props.get("science_study")


def _is_current(end_date: str | None) -> bool:
    """An allocation with no end_date is treated as open-ended/current, not
    expired. Only an explicit end_date in the past marks it as not current."""
    if not end_date:
        return True
    parsed = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > datetime.now(timezone.utc)


def _resolve_email(client: NesiClient, first_name: str, last_name: str, person_id: str) -> str | None:
    """Best-effort email lookup. The project-people endpoint does not return
    email, and there is no confirmed get-person-by-id endpoint, so this
    searches by name and only accepts an unambiguous single match on the
    same person id. Ambiguous/zero matches are logged and left unresolved —
    downstream (load.py) then falls back to the SSO-only user strategy for
    that person rather than guessing an email."""
    candidates = client.find_person(f"{first_name} {last_name}", status="All", page_size=20)
    exact = [c for c in candidates if c.get("id") == person_id]
    if len(exact) == 1:
        return exact[0].get("email")
    log.warning(
        "could not resolve email for person %s (%s %s): %d candidate(s)",
        person_id, first_name, last_name, len(exact),
    )
    return None


def extract_projects(
    client: NesiClient,
    codes: list[str],
    resolve_emails: bool = True,
    current_allocations_only: bool = False,
) -> Manifest:
    manifest = Manifest(source=f"nesi:{client.config.api_base}")
    orgs_seen: dict[str, Organization] = {}
    people_seen: dict[str, Person] = {}

    for code in codes:
        pj = client.get_project_by_code(code)

        compound_allocations: list[CompoundAllocation] = []
        for ca in client.project_compound_allocations(pj["id"]):
            components = []
            for alloc in ca.get("allocations", []):
                resource = alloc.get("resource") or {}
                resource_type = resource.get("resource_type") or {}
                facility = resource.get("facility") or {}
                components.append(
                    AllocationComponent(
                        resource_code=resource.get("code", ""),
                        resource_name=resource.get("name", ""),
                        unit=resource_type.get("unit", ""),
                        calculation_type=resource_type.get("calculation_type", ""),
                        allocated=float(alloc.get("allocated") or 0),
                        used=float(alloc["used"]) if alloc.get("used") is not None else None,
                        start_date=alloc.get("start_date"),
                        end_date=alloc.get("end_date"),
                    )
                )
            facility0 = (ca.get("allocations", [{}])[0].get("resource", {}).get("facility", {})) if ca.get("allocations") else {}
            compound_allocations.append(
                CompoundAllocation(
                    nesi_compound_allocation_id=ca["id"],
                    facility_code=facility0.get("code", ""),
                    facility_name=facility0.get("name", ""),
                    allocation_class=(ca.get("allocation_class") or {}).get("name", ""),
                    start_date=ca.get("start_date"),
                    end_date=ca.get("end_date"),
                    components=components,
                )
            )

        if current_allocations_only:
            compound_allocations = [ca for ca in compound_allocations if _is_current(ca.end_date)]
            if not compound_allocations:
                log.info("project %s: no current (non-expired) compound allocation — excluded", pj["code"])
                continue

        org = pj.get("organization") or {}
        org_id = org.get("id") or pj.get("organization_id")
        if org_id and org_id not in orgs_seen:
            orgs_seen[org_id] = Organization(nesi_org_id=org_id, name=org.get("name", ""))

        science_domain, science_study = _science_props(pj)

        members: list[ProjectMember] = []
        for row in client.project_people(pj["id"]):
            person_json = row.get("person") or {}
            pid = person_json.get("id")
            if not pid:
                continue
            members.append(ProjectMember(nesi_person_id=pid, role=row.get("role", "")))
            if pid not in people_seen:
                email = None
                if resolve_emails:
                    email = _resolve_email(
                        client, person_json.get("first_name", ""), person_json.get("last_name", ""), pid
                    )
                people_seen[pid] = Person(
                    nesi_person_id=pid,
                    email=email or "",
                    first_name=person_json.get("first_name", ""),
                    last_name=person_json.get("last_name", ""),
                    linux_username=person_json.get("linux_username", ""),
                    status=person_json.get("status", ""),
                    organization_roles={},
                )

        manifest.projects.append(
            Project(
                nesi_code=pj["code"],
                name=pj.get("name", ""),
                description=pj.get("description", ""),
                nesi_org_id=org_id or "",
                status=pj.get("status", ""),
                principal_person_id=pj.get("principal_id"),
                parent_project_code=None,  # NeSI parent_project_id is an internal numeric id;
                                            # resolving it to a code requires a lookup this
                                            # tool doesn't need for the common case. Left None;
                                            # see migration plan §4.1 (no Waldur nesting anyway).
                science_domain=science_domain,
                science_study=science_study,
                members=members,
                compound_allocations=compound_allocations,
            )
        )

    manifest.organizations = list(orgs_seen.values())
    manifest.people = list(people_seen.values())
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract a NeSI manifest for migration into Waldur.")
    parser.add_argument("--project-codes", nargs="+", help="Explicit list of NeSI project codes")
    parser.add_argument("--filter", help="Search filter to resolve a set of project codes (name/code substring)")
    parser.add_argument(
        "--organization", help="Select every project whose organization name contains this substring "
        "(case-insensitive). Pages through the full project list since the NeSI search API only "
        "filters by project name/code, not organization — slower than --filter/--project-codes on a large instance."
    )
    parser.add_argument(
        "--current-allocations-only", action="store_true",
        help="Keep only compound allocations with no end_date or an end_date in the future; "
        "drop a project entirely if it ends up with none. Excludes people/organizations that "
        "would only be referenced through an excluded project.",
    )
    parser.add_argument("--status", default="Active", help="Project status filter when using --filter/--organization (default: Active)")
    parser.add_argument("--page-size", type=int, default=100, help="Page size when using --filter/--organization")
    parser.add_argument("--out", required=True, help="Output manifest JSON path")
    parser.add_argument("--no-resolve-emails", action="store_true", help="Skip best-effort email lookup (faster, less complete)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if not args.project_codes and not args.filter and not args.organization:
        parser.error("one of --project-codes, --filter, or --organization is required")

    client = NesiClient()

    codes = args.project_codes or []
    if args.filter:
        found = client.search_projects(filter=args.filter, status=args.status, page_size=args.page_size)
        codes = codes + [p["code"] for p in found]
        log.info("filter %r matched %d project(s)", args.filter, len(found))
    if args.organization:
        needle = args.organization.lower()
        found = [
            p for p in client.iter_all_projects(status=args.status, page_size=args.page_size)
            if needle in (p.get("organization") or {}).get("name", "").lower()
        ]
        codes = codes + [p["code"] for p in found]
        log.info("organization %r matched %d project(s)", args.organization, len(found))

    codes = sorted(set(codes))
    log.info("extracting %d project(s): %s", len(codes), ", ".join(codes))

    manifest = extract_projects(
        client, codes, resolve_emails=not args.no_resolve_emails,
        current_allocations_only=args.current_allocations_only,
    )

    with open(args.out, "w") as f:
        json.dump(manifest.to_json(), f, indent=2)
    print(f"Wrote manifest with {len(manifest.projects)} project(s), "
          f"{len(manifest.people)} people, {len(manifest.organizations)} organization(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
