"""Smoke test for extract_projects against mocked NeSI API responses, using
the real response shapes captured from the live NeSI dev API during this
tool's design (project uoa04227 + its compound allocation, project nesi04233
+ its people list) — not synthetic data, so it exercises the real field
names end to end.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nesi_to_waldur.extract import extract_projects
from nesi_to_waldur.nesi_client import NesiClient
from nesi_to_waldur.nesi_config import NesiConfig

PROJECT_JSON = {
    "id": 4227,
    "code": "uoa04227",
    "name": "this is my project",
    "description": "the best project",
    "organization_id": "187022cf-992b-4e1e-893d-edb2cb9f2f6a",
    "organization": {"name": "University of Auckland", "id": "187022cf-992b-4e1e-893d-edb2cb9f2f6a"},
    "principal_id": "e3141796-afb3-4b8d-adcf-7437e7e9fa4d",
    "status": "Active",
    "properties": {"default": {"science_domain": "Formal Science", "science_study": "Software Engineering"}},
}

PEOPLE_JSON = [
    {
        "person_id": "59f3bbb3-0faa-4eff-a8f4-f2e633b9b3d3",
        "role": "Owner",
        "person": {
            "first_name": "Nathalie", "last_name": "Giraudon",
            "id": "59f3bbb3-0faa-4eff-a8f4-f2e633b9b3d3",
            "linux_username": "nathalie.girau9671", "status": "Active",
        },
    },
]

COMPOUND_ALLOC_JSON = [
    {
        "id": 10451,
        "allocation_class": {"id": 1, "name": "Collaborator"},
        "start_date": "2026-06-08T00:00:00.000Z",
        "end_date": "2026-12-01T00:00:00.000Z",
        "allocations": [
            {
                "resource_id": 30,
                "allocated": "6000.0000",
                "used": None,
                "start_date": "2026-06-08T00:00:00.000Z",
                "end_date": "2026-12-01T00:00:00.000Z",
                "resource": {
                    "code": "CPU_CORE_HPC3",
                    "name": "HPC3 Compute",
                    "resource_type": {"unit": "Units", "calculation_type": "Sum"},
                    "facility": {"id": 22, "name": "HPC3", "code": "HPC3_TDC"},
                },
            },
        ],
    }
]


def test_extract_projects_parses_real_shapes():
    client = NesiClient(config=NesiConfig(api_base="https://api.test", iam_base="https://iam.test", realm="admin", client_id="nesi-admin"))
    client.get_access_token = MagicMock(return_value="fake-token")

    with patch.object(client, "get_project_by_code", return_value=PROJECT_JSON), \
         patch.object(client, "project_people", return_value=PEOPLE_JSON), \
         patch.object(client, "project_compound_allocations", return_value=COMPOUND_ALLOC_JSON):
        manifest = extract_projects(client, ["uoa04227"], resolve_emails=False)

    assert len(manifest.organizations) == 1
    assert manifest.organizations[0].nesi_org_id == "187022cf-992b-4e1e-893d-edb2cb9f2f6a"

    assert len(manifest.people) == 1
    assert manifest.people[0].nesi_person_id == "59f3bbb3-0faa-4eff-a8f4-f2e633b9b3d3"
    assert manifest.people[0].status == "Active"

    project = manifest.projects[0]
    assert project.nesi_code == "uoa04227"
    assert project.science_domain == "Formal Science"
    assert project.members[0].role == "Owner"

    ca = project.compound_allocations[0]
    assert ca.facility_code == "HPC3_TDC"
    assert ca.allocation_class == "Collaborator"
    comp = ca.components[0]
    assert comp.resource_code == "CPU_CORE_HPC3"
    assert comp.allocated == 6000.0
    assert comp.used is None
    assert comp.calculation_type == "Sum"

    # round-trip through JSON, as extract.py's CLI would write to disk
    j = manifest.to_json()
    import json
    json.dumps(j)  # must be JSON-serializable


EXPIRED_COMPOUND_ALLOC_JSON = [
    {
        **COMPOUND_ALLOC_JSON[0],
        "id": 99999,
        "end_date": "2020-01-01T00:00:00.000Z",
        "allocations": [
            {**COMPOUND_ALLOC_JSON[0]["allocations"][0], "end_date": "2020-01-01T00:00:00.000Z"},
        ],
    }
]


def test_current_allocations_only_excludes_expired_project():
    """A project whose only compound allocation has already ended must be
    dropped entirely under --current-allocations-only, along with its
    organization/people not being pulled in by any other included project."""
    client = NesiClient(config=NesiConfig(api_base="https://api.test", iam_base="https://iam.test", realm="admin", client_id="nesi-admin"))
    client.get_access_token = MagicMock(return_value="fake-token")

    with patch.object(client, "get_project_by_code", return_value=PROJECT_JSON), \
         patch.object(client, "project_people", return_value=PEOPLE_JSON), \
         patch.object(client, "project_compound_allocations", return_value=EXPIRED_COMPOUND_ALLOC_JSON):
        manifest = extract_projects(client, ["uoa04227"], resolve_emails=False, current_allocations_only=True)

    assert manifest.projects == [], "project with only an expired allocation must be excluded"
    assert manifest.organizations == [], "org only referenced via the excluded project must not leak in"
    assert manifest.people == [], "people only referenced via the excluded project must not leak in"


def test_current_allocations_only_keeps_current_project():
    client = NesiClient(config=NesiConfig(api_base="https://api.test", iam_base="https://iam.test", realm="admin", client_id="nesi-admin"))
    client.get_access_token = MagicMock(return_value="fake-token")

    with patch.object(client, "get_project_by_code", return_value=PROJECT_JSON), \
         patch.object(client, "project_people", return_value=PEOPLE_JSON), \
         patch.object(client, "project_compound_allocations", return_value=COMPOUND_ALLOC_JSON):
        manifest = extract_projects(client, ["uoa04227"], resolve_emails=False, current_allocations_only=True)

    assert len(manifest.projects) == 1, "project with a future-dated (current) allocation must be kept"


if __name__ == "__main__":
    test_extract_projects_parses_real_shapes()
    test_current_allocations_only_excludes_expired_project()
    test_current_allocations_only_keeps_current_project()
    print("OK")
