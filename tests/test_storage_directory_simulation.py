"""Simulate waldur-site-agent's storage provisioning against a real scratch filesystem.

Observes exactly what directory paths, permissions, ownership, and quota
commands get issued before designing a Weka storage backend.

Version note: project-directory (mkdir/chmod/chown/setfacl) and homedir-quota
(ceph_xattr/xfs/lustre) support was introduced together in waldur-site-agent
1.0.4. The version actually deployed at NeSI today (0.7.0) has neither —
`_pre_create_resource` there only creates SLURM accounts, and
`create_user_homedirs` has no quota step at all. These tests therefore
simulate a **future state** (post-upgrade), not current production behavior,
pinned to 1.0.5 (the latest stable release) for reproducibility.

Weka is not a supported `homedir_quota.provider` (only ceph_xattr, xfs, and
lustre exist in `waldur_site_agent.backend.quota`). The "xfs" provider is
used here purely as a stand-in to exercise the real quota-dispatch mechanism
and capture its command shape — not to produce actual Weka CLI syntax. That
mapping is deliberately left for a follow-up once a "weka" provider exists.

Design: rather than intercepting real `chown`/`setfacl`/`mkhomedir_helper`/
`xfs_quota` binaries (most of which don't exist on this host, and the
privileged ones would fail without root), a fake client object is substituted
for `SlurmBackend.client` implementing the same `execute_command(list[str])`
interface every storage command funnels through. It really executes the safe,
unprivileged subset (`mkdir`, `chmod`) against a pytest tmp_path so directories
visibly appear on disk, and captures everything else as a command log — which
becomes the reference spec for a future Weka backend implementation.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from waldur_site_agent.backend.exceptions import BackendError
from waldur_site_agent_slurm.backend import SlurmBackend


class FakeStorageClient:
    """Capture every storage command a SlurmBackend issues.

    Really executes `mkdir`/`chmod` against the real filesystem (safe,
    unprivileged) so directories are inspectable on disk. Everything else
    (chown, setfacl, setfattr/getfattr, xfs_quota, lfs, mkhomedir_helper) is
    recorded only, never actually run against this host.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.homedir_base_path: str | None = None
        self._xattrs: dict[tuple[str, str], str] = {}

    def execute_command(self, command: list[str]) -> str:
        self.calls.append(list(command))
        name = command[0]

        if name == "mkdir":
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
            return ""
        if name == "chmod":
            mode_arg, path = command[-2], command[-1]
            target = Path(path)
            if mode_arg == "g+s":
                target.chmod(target.stat().st_mode | stat.S_ISGID)
            else:
                target.chmod(int(mode_arg, 8))
            return ""
        if name == "setfattr":
            attr, value, path = command[2], command[4], command[5]
            self._xattrs[(path, attr)] = value
            return ""
        if name == "getfattr":
            attr, path = command[3], command[4]
            return self._xattrs.get((path, attr), "")
        if name == "/sbin/mkhomedir_helper":
            username = command[1]
            if self.homedir_base_path:
                (Path(self.homedir_base_path) / username).mkdir(parents=True, exist_ok=True)
            return ""
        # chown, setfacl, xfs_quota, lfs: capture only, no real effect.
        return ""

    def create_linux_user_homedir(self, username: str, umask: str = "") -> str:
        return self.execute_command(["/sbin/mkhomedir_helper", username, umask])


def _make_backend(backend_settings: dict) -> tuple[SlurmBackend, FakeStorageClient]:
    backend = SlurmBackend(backend_settings, slurm_tres={})
    fake_client = FakeStorageClient()
    fake_client.homedir_base_path = backend_settings.get("homedir_base_path")
    backend.client = fake_client
    return backend, fake_client


def test_project_directory_creates_folder_and_captures_commands(tmp_path):
    """Create a real directory and capture the chown/setfacl commands.

    `_setup_project_directory` should mkdir/chmod for real and capture the
    chown/setfacl commands it would issue in production.
    """
    base_path = tmp_path / "projects"
    backend, client = _make_backend(
        {
            "default_account": "root",
            "project_directory": {
                "enabled": True,
                "base_path": str(base_path),
                "owner": "nobody",
                "permissions": "770",
                "set_gid": True,
                "set_acl": True,
            },
        }
    )

    backend._setup_project_directory("landcare00034")

    project_path = base_path / "landcare00034"
    assert project_path.is_dir(), "project directory should really exist on disk"
    mode = stat.S_IMODE(project_path.stat().st_mode)
    assert mode & 0o770 == 0o770, f"expected at least 0770 permissions, got {oct(mode)}"
    assert mode & stat.S_ISGID, "set_gid should have set the setgid bit"

    assert client.calls == [
        ["mkdir", "-p", str(project_path)],
        ["chmod", "770", str(project_path)],
        ["chmod", "g+s", str(project_path)],
        ["chown", "nobody:landcare00034", str(project_path)],
        [
            "setfacl",
            "-R",
            "-m",
            "group:landcare00034:rwx,d:group:landcare00034:rwx",
            str(project_path),
        ],
    ], "captured command log doubles as the spec a Weka backend must replicate"


def test_project_directory_skips_lustre_quota_without_ldap(tmp_path):
    """Confirm Lustre project quota no-ops without LDAP, needing no fake LDAP.

    Lustre project quota silently no-ops without an LDAP client configured
    (documented behavior).
    """
    base_path = tmp_path / "projects"
    backend, client = _make_backend(
        {
            "default_account": "root",
            "project_directory": {
                "enabled": True,
                "base_path": str(base_path),
                "lustre_quota": {"mount_point": "/valhalla", "block_softlimit": 100},
            },
        }
    )

    backend._setup_project_directory("landcare00034")

    assert not any(c[0] == "lfs" for c in client.calls), (
        "no LDAP client configured -> GID lookup skipped -> no lfs commands"
    )


def test_homedir_creation_and_stub_quota_capture(tmp_path):
    """Create a real home directory and capture the quota commands.

    Uses the "xfs" provider as a stand-in only, since "weka" is not yet a
    supported provider — this captures the mechanism's command shape, not
    real Weka CLI syntax.
    """
    home_base = tmp_path / "home"
    backend, client = _make_backend(
        {
            "default_account": "root",
            "homedir_base_path": str(home_base),
            "enable_user_homedir_account_creation": True,
            "default_homedir_umask": "0077",
            "homedir_quota": {
                "provider": "xfs",
                "mount_point": "/weka/home",
                "block_softlimit": "900g",
                "block_hardlimit": "1t",
                "inode_softlimit": 90000,
                "inode_hardlimit": 100000,
            },
        }
    )

    backend.create_user_homedirs({"jdoe"}, umask="0077")

    home_path = home_base / "jdoe"
    assert home_path.is_dir(), "home directory should really exist on disk"

    assert ["/sbin/mkhomedir_helper", "jdoe", "0077"] in client.calls
    assert [
        "xfs_quota",
        "-x",
        "-c",
        "limit -u bsoft=900g bhard=1t isoft=90000 ihard=100000 jdoe",
        "/weka/home",
    ] in client.calls
    assert [
        "xfs_quota",
        "-x",
        "-c",
        "quota -u -N -b -h jdoe",
        "/weka/home",
    ] in client.calls


def test_project_directory_reports_backend_error_without_crashing(tmp_path):
    """Confirm a directory-creation failure doesn't crash the caller.

    A failure (e.g. a real chown/setfacl error in production) must not
    propagate past `_setup_project_directory` — the method logs and returns
    rather than raising, per its own try/except.
    """

    class FailingClient(FakeStorageClient):
        def execute_command(self, command: list[str]) -> str:
            if command[0] == "chown":
                msg = "simulated permission denied"
                raise BackendError(msg)
            return super().execute_command(command)

    base_path = tmp_path / "projects"
    backend = SlurmBackend(
        {
            "default_account": "root",
            "project_directory": {"enabled": True, "base_path": str(base_path)},
        },
        slurm_tres={},
    )
    backend.client = FailingClient()

    backend._setup_project_directory("landcare00034")  # must not raise

    assert (base_path / "landcare00034").is_dir(), (
        "mkdir/chmod happen before the failing chown, so the directory "
        "still exists even though ownership setup failed"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
