from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from typing import Self

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "dev"
LOADER = importlib.machinery.SourceFileLoader("oddish_dev_launcher", str(SCRIPT_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
dev = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dev
LOADER.exec_module(dev)


def _valid_env() -> str:
    return (
        "E2E_CLERK_EMAIL=developer+clerk_test@example.com\n"
        "E2E_CLERK_ORG_ID=org_dev\n"
        "CLERK_SECRET_KEY=sk_test_secret\n"
        "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_publishable\n"
        "CLERK_DOMAIN=prepared-otter-1.clerk.accounts.dev"
    )


def test_ports_are_deterministic_and_separated_by_service_band() -> None:
    first = dev.derive_ports("worktree-identity")
    second = dev.derive_ports("worktree-identity")

    assert first == second
    assert 20000 <= first.postgres < 25000
    assert 25000 <= first.minio < 30000
    assert 30000 <= first.minio_console < 35000
    assert 35000 <= first.backend < 40000
    assert 40000 <= first.frontend < 45000
    assert len(set(dev.asdict(first).values())) == 5


def test_cell_identity_does_not_change_with_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branches = iter(("feature/one", "feature/two"))
    monkeypatch.setattr(dev, "_git_branch", lambda _root: next(branches))

    first = dev.discover_cell(tmp_path)
    second = dev.discover_cell(tmp_path)

    assert first.id == second.id
    assert first.ports == second.ports
    assert first.branch != second.branch


def test_load_dev_env_accepts_one_explicit_well_formed_file(tmp_path: Path) -> None:
    path = tmp_path / "dev.env"
    path.write_text("# local only\n" + _valid_env() + "\nOPTIONAL='quoted value'\n")
    path.chmod(0o600)

    values = dev.load_dev_env(path)

    assert values["E2E_CLERK_ORG_ID"] == "org_dev"
    assert values["OPTIONAL"] == "quoted value"


def test_load_dev_env_rejects_public_permissions(tmp_path: Path) -> None:
    path = tmp_path / "dev.env"
    path.write_text(_valid_env())
    path.chmod(0o644)

    with pytest.raises(dev.DevError, match="chmod 600"):
        dev.load_dev_env(path)


@pytest.mark.parametrize(
    ("line", "message"),
    (
        ("CLERK_SECRET_KEY=sk_live_secret", "development instance"),
        ("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_key", "development instance"),
        ("CLERK_DOMAIN=https://clerk.example.com/path", "bare hostname"),
    ),
)
def test_load_dev_env_rejects_unsafe_clerk_values(
    tmp_path: Path, line: str, message: str
) -> None:
    path = tmp_path / "dev.env"
    lines = _valid_env().splitlines()
    key = line.split("=", 1)[0]
    path.write_text(
        "\n".join(line if value.startswith(f"{key}=") else value for value in lines)
    )
    path.chmod(0o600)

    with pytest.raises(dev.DevError, match=message):
        dev.load_dev_env(path)


def test_state_round_trip_validates_disk_boundary(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = dev.State(
        schema_version=1,
        cell_id="oddish-12345678",
        repo_root=str(REPO_ROOT),
        branch="feature/local-dev",
        compose_project="oddish-oddish-12345678",
        status="running",
        ports=dev.derive_ports("state"),
        processes={
            "backend": dev.ProcessRecord(pid=100, pgid=100, started_at="Thu Aug 13"),
            "frontend": dev.ProcessRecord(pid=101, pgid=101, started_at="Thu Aug 13"),
        },
    )

    dev.save_state(state, path)

    assert dev.load_state(path) == state
    assert path.stat().st_mode & 0o777 == 0o600


def test_state_rejects_unknown_process_owner(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cell_id": "cell",
                "repo_root": str(REPO_ROOT),
                "branch": "branch",
                "compose_project": "project",
                "status": "running",
                "ports": dev.asdict(dev.derive_ports("state")),
                "processes": {
                    "mystery": {"pid": 100, "pgid": 100, "started_at": "sometime"}
                },
            }
        )
    )

    with pytest.raises(dev.DevError, match="unknown processes: mystery"):
        dev.load_state(path)


def test_process_ownership_includes_start_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dev.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(dev, "process_signature", lambda _pid: "expected process")
    current = dev.ProcessRecord(pid=100, pgid=100, started_at="expected process")
    reused = dev.ProcessRecord(pid=100, pgid=100, started_at="different process")

    assert dev.process_is_owned(current)
    assert not dev.process_is_owned(reused)


def test_lifecycle_lock_refuses_a_second_owner(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"

    with dev.lifecycle_lock(path):
        with pytest.raises(dev.DevError, match="lifecycle command"):
            with dev.lifecycle_lock(path):
                pass


def test_running_state_requires_both_processes_and_data_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cell = dev.Cell("cell", "branch", "oddish-cell", dev.derive_ports("cell"))
    backend = dev.ProcessRecord(pid=100, pgid=100, started_at="backend")
    frontend = dev.ProcessRecord(pid=101, pgid=101, started_at="frontend")
    services = {"postgres"}
    monkeypatch.setattr(dev, "process_is_owned", lambda _record: True)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *_args, **_kwargs: dev.subprocess.CompletedProcess(
            [], 0, stdout="\n".join(sorted(services))
        ),
    )

    assert not dev.cell_is_running(
        dev.state_for(cell, "running", {"backend": backend}), cell
    )
    running = dev.state_for(cell, "running", {"backend": backend, "frontend": frontend})
    assert not dev.cell_is_running(running, cell)

    services.add("minio")
    assert dev.cell_is_running(
        running,
        cell,
    )


def test_runtime_env_isolates_browser_origin_and_cloud_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "ambient-secret")
    cell = dev.Cell("cell", "branch", "oddish-cell", dev.derive_ports("cell"))

    environment = dev.runtime_env(cell, {})

    assert environment["NEXT_PUBLIC_APP_URL"].startswith("http://cell.localhost:")
    assert environment["NEXT_PUBLIC_API_URL"].startswith("http://api-cell.localhost:")
    assert environment["ODDISH_CLOUD_CONTROL_PLANE_ENABLED"] == "false"
    assert "MODAL_TOKEN_ID" not in environment


def test_port_conflict_names_the_service_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 20001

    class FakeSocket:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def setsockopt(self, *_args: object) -> None:
            return None

        def bind(self, address: tuple[str, int]) -> None:
            if address[1] == port:
                raise OSError("occupied")

    monkeypatch.setattr(dev.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(dev, "_port_owner", lambda _port: "test-listener PID 123")
    ports = dev.Ports(
        postgres=port,
        minio=25001,
        minio_console=30001,
        backend=35001,
        frontend=40001,
    )

    with pytest.raises(dev.DevError, match=rf"postgres port {port}.*PID 123"):
        dev.assert_ports_available(ports)
