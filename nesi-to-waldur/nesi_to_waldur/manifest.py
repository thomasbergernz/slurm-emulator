"""Normalized intermediate representation between NeSI extraction and Waldur loading.

Keeping this as a plain-JSON-serializable schema (not tied to either side's API
shapes) is what makes the pipeline repeatable: `extract` can be re-run against
any set of NeSI project codes to produce a new manifest file, and `load` can
be re-run against any manifest — including one edited by hand, or produced by
a future non-NeSI source — without either side knowing about the other.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Organization:
    nesi_org_id: str
    name: str


@dataclass
class Person:
    nesi_person_id: str  # Keycloak UUID
    email: str
    first_name: str
    last_name: str
    linux_username: str
    status: str  # Active / Pending / Pending_Approval
    organization_roles: dict = field(default_factory=dict)  # {nesi_org_id: organizational_role}


@dataclass
class ProjectMember:
    nesi_person_id: str
    role: str  # raw NeSI role string, e.g. "Owner", "Principal Investigator"


@dataclass
class AllocationComponent:
    resource_code: str  # e.g. CPU_CORE_HPC3, WEKA_STORAGE_SPACE_PROJECT
    resource_name: str
    unit: str  # GiB, Units
    calculation_type: str  # Sum, Last
    allocated: float
    used: float | None
    start_date: str | None
    end_date: str | None


@dataclass
class CompoundAllocation:
    nesi_compound_allocation_id: int
    facility_code: str
    facility_name: str
    allocation_class: str
    start_date: str | None
    end_date: str | None
    components: list = field(default_factory=list)  # list[AllocationComponent]


@dataclass
class Project:
    nesi_code: str
    name: str
    description: str
    nesi_org_id: str
    status: str
    principal_person_id: str | None
    parent_project_code: str | None
    science_domain: str | None
    science_study: str | None
    members: list = field(default_factory=list)  # list[ProjectMember]
    compound_allocations: list = field(default_factory=list)  # list[CompoundAllocation]


@dataclass
class Manifest:
    """Top-level container. `source` records where this manifest came from,
    so a `load` run's report can cite it back."""

    source: str
    organizations: list = field(default_factory=list)  # list[Organization]
    people: list = field(default_factory=list)  # list[Person]
    projects: list = field(default_factory=list)  # list[Project]

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(data: dict) -> "Manifest":
        return Manifest(
            source=data["source"],
            organizations=[Organization(**o) for o in data.get("organizations", [])],
            people=[Person(**p) for p in data.get("people", [])],
            projects=[
                Project(
                    **{
                        **p,
                        "members": [ProjectMember(**m) for m in p.get("members", [])],
                        "compound_allocations": [
                            CompoundAllocation(
                                **{
                                    **ca,
                                    "components": [
                                        AllocationComponent(**c) for c in ca.get("components", [])
                                    ],
                                }
                            )
                            for ca in p.get("compound_allocations", [])
                        ],
                    }
                )
                for p in data.get("projects", [])
            ],
        )
