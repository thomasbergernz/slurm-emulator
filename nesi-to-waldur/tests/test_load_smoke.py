"""Smoke test for load_manifest against a mocked Waldur API — no network,
no real Waldur instance required. Exercises: idempotent customer/project
creation, the missing-Offering-UUID skip path, and the unknown-role warning
path (the three most likely ways a second, different manifest could surprise
this pipeline).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("WALDUR_API_URL", "https://waldur.example.test")
os.environ.setdefault("WALDUR_API_TOKEN", "test-token")

from nesi_to_waldur.load import LoadReport, load_crosswalks, load_manifest  # noqa: E402
from nesi_to_waldur.manifest import (  # noqa: E402
    AllocationComponent,
    CompoundAllocation,
    Manifest,
    Organization,
    Person,
    Project,
    ProjectMember,
)
from nesi_to_waldur.waldur_client import WaldurClient  # noqa: E402


# Hermetic crosswalk fixtures — NOT loaded from the live crosswalks/*.yaml
# (which carry real deployment UUIDs and change over time). Keeps these unit
# tests deterministic regardless of the deployed config.
CROSSWALKS_NO_OFFERING = {
    "roles": {
        "project_roles": {"Owner": "PROJECT.ADMIN"},
        "default_project_role": "PROJECT.MEMBER",
        "customer_roles": {"default": "owner"},
    },
    "oecd": {"science_domain_to_oecd": {"Applied Science": None}},
    "resources": {
        "facilities": {"HPC3_TDC": {"facility_name": "HPC3", "offering_uuid": None, "plans": {"Collaborator": None}}},
        "components": {"CPU_CORE_HPC3": {"component_type": "cpu", "measured_unit": "core-hours", "billing_type": "limit"}},
    },
}
CROSSWALKS_WITH_OFFERING = {
    **CROSSWALKS_NO_OFFERING,
    "resources": {
        "facilities": {"HPC3_TDC": {"facility_name": "HPC3", "offering_uuid": "off-uuid", "plans": {"Collaborator": "plan-uuid"}}},
        "components": {"CPU_CORE_HPC3": {"component_type": "cpu", "measured_unit": "core-hours", "billing_type": "limit"}},
    },
}


def _resp(status=200, json_body=None):
    m = MagicMock()
    m.status_code = status
    m.ok = status < 400
    m.content = b"1" if json_body is not None else b""
    m.json.return_value = json_body if json_body is not None else {}
    m.raise_for_status = MagicMock()
    return m


def build_manifest() -> Manifest:
    m = Manifest(source="test")
    m.organizations.append(Organization(nesi_org_id="org-1", name="Org One"))
    m.people.append(Person(nesi_person_id="p1", email="a@b.com", first_name="A", last_name="B", linux_username="ab", status="Active"))
    m.projects.append(
        Project(
            nesi_code="uoa00001", name="Test Project", description="d", nesi_org_id="org-1",
            status="Active", principal_person_id="p1", parent_project_code=None,
            science_domain="Applied Science", science_study="x",
            members=[ProjectMember(nesi_person_id="p1", role="Nonexistent Role")],
            compound_allocations=[
                CompoundAllocation(
                    nesi_compound_allocation_id=1, facility_code="HPC3_TDC", facility_name="HPC3",
                    allocation_class="Collaborator", start_date=None, end_date=None,
                    components=[
                        AllocationComponent(
                            resource_code="CPU_CORE_HPC3", resource_name="HPC3 Compute", unit="Units",
                            calculation_type="Sum", allocated=6000.0, used=None, start_date=None, end_date=None,
                        )
                    ],
                )
            ],
        )
    )
    return m


def test_load_manifest_creates_and_warns():
    manifest = build_manifest()

    def fake_get(url, headers=None, params=None, timeout=None):
        # empty results everywhere -> nothing pre-exists -> everything gets created
        if "/api/users/" in url:
            return _resp(json_body=[{"uuid": "user-uuid", "email": "a@b.com"}])
        if "/api/customers/" in url or "/api/projects/" in url or "/api/marketplace" in url:
            return _resp(json_body=[])
        if url.endswith("/list_users/"):
            return _resp(json_body=[])
        return _resp(json_body=[])

    def fake_request(method, url, headers=None, json=None, timeout=None):
        if url.endswith("/api/customers/"):
            return _resp(json_body={"uuid": "cust-uuid", "url": url + "cust-uuid/"})
        if url.endswith("/api/projects/"):
            return _resp(json_body={"uuid": "proj-uuid", "url": url + "proj-uuid/"})
        return _resp(json_body={})

    with patch("requests.get", side_effect=fake_get), patch("requests.request", side_effect=fake_request):
        client = WaldurClient(dry_run=False)
        report = LoadReport()
        load_manifest(client, manifest, CROSSWALKS_NO_OFFERING, user_strategy="sso-only", report=report)

    action_paths = [a["path"] for a in client.actions]
    assert "/api/customers/" in action_paths, "customer should have been created"
    assert "/api/projects/" in action_paths, "project should have been created"

    assert any("Nonexistent Role" in w for w in report.warnings), "unknown role should warn, not silently misassign"
    assert any("no configured Offering UUID" in w for w in report.warnings), "null offering_uuid in crosswalk should skip, not crash"


def test_load_manifest_creates_resource_via_order_lifecycle():
    """With a configured offering, the resource is created via the order
    lifecycle: order POST -> approve_by_provider -> set_state_done ->
    set_backend_id, with limits sent inline on the order."""
    manifest = build_manifest()
    order_uuid = "order-uuid"

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/api/users/" in url:
            return _resp(json_body=[{"uuid": "user-uuid", "email": "a@b.com"}])
        if f"/api/marketplace-orders/{order_uuid}/" in url:
            return _resp(json_body={"uuid": order_uuid, "state": "done"})  # already done after actions
        if url.endswith("/list_users/"):
            return _resp(json_body=[])
        return _resp(json_body=[])  # nothing pre-exists (no resource with backend_id yet)

    order_state = {"state": "pending-provider"}

    def fake_get_stateful(url, headers=None, params=None, timeout=None):
        if "/api/users/" in url:
            return _resp(json_body=[{"uuid": "user-uuid", "email": "a@b.com"}])
        if f"/api/marketplace-orders/{order_uuid}/" in url:
            return _resp(json_body={"uuid": order_uuid, "state": order_state["state"]})
        if url.endswith("/marketplace-provider-offerings/off-uuid/"):
            return _resp(json_body={"uuid": "off-uuid"})  # get_offering -> truthy
        if url.endswith("/list_users/"):
            return _resp(json_body=[])
        return _resp(json_body=[])  # customers/projects/resource-by-backend_id all empty

    def fake_request(method, url, headers=None, json=None, timeout=None):
        if url.endswith("/api/customers/"):
            return _resp(json_body={"uuid": "cust-uuid", "url": url + "cust-uuid/"})
        if url.endswith("/api/projects/"):
            return _resp(json_body={"uuid": "proj-uuid", "url": url + "proj-uuid/"})
        if url.endswith("/api/marketplace-orders/"):
            return _resp(json_body={"uuid": order_uuid, "state": "pending-provider", "marketplace_resource_uuid": "res-uuid"})
        if url.endswith("/approve_by_provider/"):
            order_state["state"] = "executing"
            return _resp(json_body={})
        if url.endswith("/set_state_done/"):
            order_state["state"] = "done"
            return _resp(json_body={})
        return _resp(json_body={})

    with patch("requests.get", side_effect=fake_get_stateful), patch("requests.request", side_effect=fake_request):
        client = WaldurClient(dry_run=False)
        report = LoadReport()
        load_manifest(client, manifest, CROSSWALKS_WITH_OFFERING, user_strategy="sso-only", report=report)

    paths = [a["path"] for a in client.actions]
    assert "/api/marketplace-orders/" in paths, "an order should have been created"
    assert any(p.endswith("/approve_by_provider/") for p in paths), "order should be provider-approved"
    assert any(p.endswith("/set_state_done/") for p in paths), "order should be marked done"
    assert any(p.endswith("/set_backend_id/") for p in paths), "resource backend_id should be stamped for idempotency"
    # limits sent inline on the order, as ints
    order_action = next(a for a in client.actions if a["path"] == "/api/marketplace-orders/")
    assert order_action["body"]["limits"] == {"cpu": 6000}, "allocated amount should be the inline limit (int)"


def test_load_manifest_dry_run_does_not_crash_and_still_reports():
    """Regression test: dry-run must not stop at the first create. Before the
    fix, `_mutate` returned a bare `{"dry_run": True}` with no uuid/url, so
    `_customer_url`/`_project_url` KeyError'd as soon as any project's
    customer was created in the same dry run — i.e. every fresh migration."""
    manifest = build_manifest()

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/api/users/" in url:
            return _resp(json_body=[{"uuid": "user-uuid", "email": "a@b.com"}])
        return _resp(json_body=[])  # nothing else pre-exists; also covers the
        # dry-run list_users/offering GETs against synthetic dry-run objects

    with patch("requests.get", side_effect=fake_get):
        client = WaldurClient(dry_run=True)
        report = LoadReport()
        load_manifest(client, manifest, CROSSWALKS_NO_OFFERING, user_strategy="sso-only", report=report)

    action_paths = [a["path"] for a in client.actions]
    assert "/api/customers/" in action_paths
    assert "/api/projects/" in action_paths
    # membership/resource logic must still execute in dry-run, not bail out
    # after the project create — this is what makes the dry-run report useful
    assert any("Nonexistent Role" in w for w in report.warnings)
    assert any("no configured Offering UUID" in w for w in report.warnings)


def test_load_crosswalks_file_is_valid():
    """The real crosswalks/*.yaml must load (structure intact), even though the
    other tests use hermetic fixtures rather than these live values."""
    cw = load_crosswalks()
    assert "project_roles" in cw["roles"]
    assert "facilities" in cw["resources"]
    assert "components" in cw["resources"]


if __name__ == "__main__":
    test_load_manifest_creates_and_warns()
    test_load_manifest_creates_resource_via_order_lifecycle()
    test_load_manifest_dry_run_does_not_crash_and_still_reports()
    test_load_crosswalks_file_is_valid()
    print("OK")
