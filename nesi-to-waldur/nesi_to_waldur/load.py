"""Idempotently apply a manifest (produced by extract.py) to a Waldur instance.

Re-running this against the SAME manifest is a no-op (everything is keyed by
backend_id / existing-permission checks). Re-running it against a NEW
manifest (a different project set) requires no code changes — that's the
repeatability this tool is built around. Crosswalk decisions (roles, OECD FoS
codes, offering/plan identity) live in migration/crosswalks/*.yaml, not in
this file, so correcting them doesn't require touching code either.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

from .manifest import Manifest, Project
from .waldur_client import WaldurClient

log = logging.getLogger("nesi_to_waldur.load")

CROSSWALK_DIR = Path(__file__).resolve().parent.parent / "crosswalks"


def load_crosswalks() -> dict:
    with open(CROSSWALK_DIR / "roles.yaml") as f:
        roles = yaml.safe_load(f)
    with open(CROSSWALK_DIR / "oecd_fos.yaml") as f:
        oecd = yaml.safe_load(f)
    with open(CROSSWALK_DIR / "resource_components.yaml") as f:
        resources = yaml.safe_load(f)
    return {"roles": roles, "oecd": oecd, "resources": resources}


class LoadReport:
    def __init__(self):
        self.warnings: list[str] = []
        self.skipped: list[str] = []

    def warn(self, message: str) -> None:
        log.warning(message)
        self.warnings.append(message)

    def skip(self, message: str) -> None:
        log.info("skip: %s", message)
        self.skipped.append(message)


def _customer_url(client: WaldurClient, base_url: str, customer: dict) -> str:
    return customer.get("url") or f"{base_url}/api/customers/{customer['uuid']}/"


def _project_url(base_url: str, project: dict) -> str:
    return project.get("url") or f"{base_url}/api/projects/{project['uuid']}/"


def load_manifest(
    client: WaldurClient,
    manifest: Manifest,
    crosswalks: dict,
    user_strategy: str,
    report: LoadReport,
) -> None:
    base_url = client.base_url

    # 1. organizations
    customer_by_org_id: dict[str, dict] = {}
    for org in manifest.organizations:
        customer = client.ensure_customer(org.name, org.nesi_org_id)
        customer_by_org_id[org.nesi_org_id] = customer

    # 2. people -> Waldur user resolution (no creation here unless pre-create strategy)
    waldur_user_by_person_id: dict[str, str] = {}
    for person in manifest.people:
        if person.status != "Active":
            report.skip(f"person {person.nesi_person_id} status={person.status!r} — not imported")
            continue
        if not person.email:
            report.warn(f"person {person.nesi_person_id} ({person.first_name} {person.last_name}) has no resolved email — cannot match/create Waldur user")
            continue
        existing = client.find_user_by_email(person.email)
        if existing:
            waldur_user_by_person_id[person.nesi_person_id] = existing["uuid"]
        elif user_strategy == "pre-create":
            username = person.nesi_person_id  # only valid if target shares NeSI's Keycloak realm — see plan §4.2
            created = client.create_user_stub(username, person.email, person.first_name, person.last_name)
            if not client.dry_run:
                waldur_user_by_person_id[person.nesi_person_id] = created["uuid"]
        else:
            report.skip(f"person {person.email} has no Waldur account yet — will resolve on first SSO login (sso-only strategy)")

    # 3. projects
    role_map = crosswalks["roles"]["project_roles"]
    default_role = crosswalks["roles"]["default_project_role"]
    oecd_map = crosswalks["oecd"]["science_domain_to_oecd"]
    facilities = crosswalks["resources"]["facilities"]
    components = crosswalks["resources"]["components"]

    for project in manifest.projects:
        customer = customer_by_org_id.get(project.nesi_org_id)
        if not customer:
            report.warn(f"project {project.nesi_code} references unknown organization {project.nesi_org_id} — skipped")
            continue

        oecd_code = None
        if project.science_domain is not None:
            if project.science_domain in oecd_map:
                oecd_code = oecd_map[project.science_domain]
            else:
                report.warn(f"project {project.nesi_code}: science_domain {project.science_domain!r} has no crosswalk entry in oecd_fos.yaml")
        if project.science_domain and oecd_map.get(project.science_domain) is None:
            report.skip(f"project {project.nesi_code}: oecd_fos_2007_code not set (crosswalk entry is null)")

        waldur_project = client.ensure_project(
            name=project.name,
            description=project.description,
            customer_url=_customer_url(client, base_url, customer),
            backend_id=project.nesi_code,
            oecd_fos_2007_code=oecd_code,
        )
        # In dry-run, ensure_project/ensure_customer return synthetic
        # uuid/url placeholders (see WaldurClient._mutate) specifically so
        # membership + resource logic below still runs and reports what it
        # *would* do, instead of stopping at the first create.
        project_url = _project_url(base_url, waldur_project)

        # membership
        for member in project.members:
            waldur_user_uuid = waldur_user_by_person_id.get(member.nesi_person_id)
            if not waldur_user_uuid:
                report.skip(f"project {project.nesi_code}: member {member.nesi_person_id} has no resolved Waldur account — permission not granted")
                continue
            role = role_map.get(member.role)
            if role is None:
                report.warn(f"project {project.nesi_code}: role {member.role!r} has no crosswalk entry — using default {default_role}")
                role = default_role
            client.add_project_user(waldur_project["uuid"], waldur_user_uuid, role)

        # resources
        for ca in project.compound_allocations:
            facility_cfg = facilities.get(ca.facility_code)
            if not facility_cfg or not facility_cfg.get("offering_uuid"):
                report.warn(f"project {project.nesi_code}: facility {ca.facility_code!r} has no configured Offering UUID in resource_components.yaml — allocation {ca.nesi_compound_allocation_id} skipped")
                continue
            plan_uuid = (facility_cfg.get("plans") or {}).get(ca.allocation_class)
            if not plan_uuid:
                report.warn(f"project {project.nesi_code}: allocation_class {ca.allocation_class!r} on facility {ca.facility_code!r} has no configured Plan UUID — allocation {ca.nesi_compound_allocation_id} skipped")
                continue

            offering = client.get_offering(facility_cfg["offering_uuid"])
            if not offering:
                report.warn(f"project {project.nesi_code}: configured Offering uuid={facility_cfg['offering_uuid']!r} not found on target Waldur instance — allocation {ca.nesi_compound_allocation_id} skipped")
                continue

            # Build the limits map (component_type -> allocated) from the
            # crosswalk. Waldur only accepts limit-based component types here,
            # so all crosswalked HPC3 components are configured as limit-based
            # on the offering (see resource_components.yaml).
            limits = {}
            for comp in ca.components:
                comp_cfg = components.get(comp.resource_code)
                if not comp_cfg:
                    report.warn(f"allocation {ca.nesi_compound_allocation_id}: resource_code {comp.resource_code!r} has no crosswalk entry in resource_components.yaml — component skipped")
                    continue
                # Waldur limits are integers; NeSI allocated is a float amount.
                limits[comp_cfg["component_type"]] = int(comp.allocated)
            if not limits:
                report.warn(f"allocation {ca.nesi_compound_allocation_id}: no crosswalked components — resource not created")
                continue

            resource_backend_id = f"nesi-alloc-{ca.nesi_compound_allocation_id}"
            resource_name = f"{project.nesi_code} {ca.facility_name} allocation"
            client.create_resource_via_order(
                offering["uuid"], waldur_project["uuid"], plan_uuid,
                resource_backend_id, resource_name, limits,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load a NeSI manifest into Waldur.")
    parser.add_argument("--manifest", required=True, help="Manifest JSON path (from extract.py)")
    parser.add_argument("--dry-run", action="store_true", help="Log every action without calling Waldur's write endpoints")
    parser.add_argument(
        "--user-strategy", choices=["sso-only", "pre-create"], default="sso-only",
        help="sso-only (default, safe): grant permissions only for people with an existing Waldur account, "
             "let others self-provision at first login. pre-create: create stub accounts for unmatched Active "
             "people — only use this if the target instance shares NeSI's Keycloak realm (plan §4.2).",
    )
    parser.add_argument("--report-out", help="Write the JSON action/warning report to this path (default: print to stdout)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    with open(args.manifest) as f:
        manifest = Manifest.from_json(json.load(f))

    crosswalks = load_crosswalks()
    client = WaldurClient(dry_run=args.dry_run)
    report = LoadReport()

    load_manifest(client, manifest, crosswalks, args.user_strategy, report)

    output = {
        "manifest_source": manifest.source,
        "dry_run": args.dry_run,
        "actions": client.actions,
        "warnings": report.warnings,
        "skipped": report.skipped,
    }
    text = json.dumps(output, indent=2)
    if args.report_out:
        Path(args.report_out).write_text(text)
        print(f"Wrote report to {args.report_out} "
              f"({len(client.actions)} action(s), {len(report.warnings)} warning(s), {len(report.skipped)} skipped)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
