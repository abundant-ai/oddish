from __future__ import annotations

import importlib
import tomllib
from importlib.metadata import version
from pathlib import Path

from botocore.session import get_session


ROOT = Path(__file__).resolve().parents[2]


def test_worker_uses_aioboto3_compatible_direct_boto3_pin() -> None:
    project = tomllib.loads((ROOT / "oddish" / "pyproject.toml").read_text())
    worker = project["project"]["optional-dependencies"]["worker"]

    assert "boto3==1.40.61" in worker
    assert version("boto3") == "1.40.61"


def test_direct_boto3_strategy_activates_harbor_ec2_and_has_required_apis() -> None:
    harbor_ec2 = importlib.import_module("harbor.environments.ec2")
    service = get_session().get_service_model("ec2")

    assert harbor_ec2._HAS_BOTO3 is True
    assert service.operation_model("RunInstances")
    assert service.operation_model("DescribeInstances")
    assert service.operation_model("TerminateInstances")


def test_worker_images_install_openssh_client() -> None:
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text()
    modal_app = (ROOT / "backend" / "modal_app.py").read_text()

    assert "openssh-client" in dockerfile
    assert '"openssh-client"' in modal_app


def test_operator_docs_cover_sts_and_standalone_openssh() -> None:
    backend_env = (ROOT / "backend" / ".env.example").read_text()
    backend_readme = (ROOT / "backend" / "README.md").read_text()
    oddish_env = (ROOT / "oddish" / "env.example").read_text()

    assert "sts:GetCallerIdentity" in backend_env
    assert "openssh-client" in backend_readme
    assert "openssh-client" in oddish_env
