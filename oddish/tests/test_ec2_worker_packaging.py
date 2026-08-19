from __future__ import annotations

import importlib

from botocore.session import get_session


def test_direct_boto3_strategy_activates_harbor_ec2_and_has_required_apis() -> None:
    harbor_ec2 = importlib.import_module("harbor.environments.ec2")
    service = get_session().get_service_model("ec2")

    assert harbor_ec2._HAS_BOTO3 is True
    assert service.operation_model("RunInstances")
    assert service.operation_model("DescribeInstances")
    assert service.operation_model("TerminateInstances")
