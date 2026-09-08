from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from auth import require_admin, require_auth
from fastapi import HTTPException

_ROUTER_PATH = Path(__file__).resolve().parents[1] / "api" / "routers" / "deliveries.py"
_SPEC = spec_from_file_location("deliveries_route_under_test", _ROUTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
deliveries = module_from_spec(_SPEC)
_SPEC.loader.exec_module(deliveries)


def test_readiness_mutations_require_admin_and_work_coordination_requires_auth() -> (
    None
):
    """Only work coordination is available to ordinary task-scoped members."""
    mutations = 0
    for route in deliveries.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if not (methods - {"GET", "HEAD", "OPTIONS"}):
            continue
        mutations += 1
        dependant = getattr(route, "dependant", None)
        assert dependant is not None
        calls = [d.call for d in dependant.dependencies]
        required = (
            require_auth
            if route.path
            in {
                "/deliveries/{delivery_id}/qa-work",
                "/deliveries/{delivery_id}/qa-work/claim",
            }
            else require_admin
        )
        assert required in calls, (
            f"{route.path} {methods} is a mutation without require_admin"
        )
    assert mutations >= 5  # the router actually carries its mutations


def test_fill_user_names_fallback_chain() -> None:
    """Display name resolution: name, else @handle, else email local part."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from oddish.schemas import (
        DeliveryBoardResponse,
        DeliveryCheckConfig,
        DeliveryCheckResult,
        DeliveryDefect,
        DeliveryResponse,
        DeliveryTaskBoardRow,
        QAWorkMetadata,
    )

    def check(key: str, user_id: str) -> DeliveryCheckResult:
        return DeliveryCheckResult(
            key=key,
            kind="manual",
            label=key,
            status="pass",
            checked_by_user_id=user_id,
        )

    board = DeliveryBoardResponse(
        delivery=DeliveryResponse(
            id="d1",
            name="d",
            customer_name=None,
            description=None,
            status="active",
            is_public=False,
            finalized_at=None,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ),
        check_config=DeliveryCheckConfig(),
        tasks=[
            DeliveryTaskBoardRow(
                delivery_task_id="dt1",
                task_id="t1",
                task_name="t1",
                version_id="v1",
                version=1,
                pinned_version_id=None,
                newer_version_exists=False,
                is_visible=True,
                sort_order=0,
                customer_note=None,
                internal_note=None,
                qa_work=QAWorkMetadata(owner_user_id="u-named"),
                checks=[check("signoff", "u-named"), check("proof", "u-email")],
                defects=[
                    DeliveryDefect(
                        id="def1",
                        title="x",
                        source="trial",
                        acknowledged=True,
                        acknowledged_by_user_id="u-handle",
                    )
                ],
                ready=True,
            )
        ],
        delivery_checks=[check("scope_ok", "u-missing")],
        ready=True,
        ready_task_count=1,
        task_count=1,
    )

    rows = MagicMock()
    rows.all.return_value = [
        ("u-named", "Ada Lovelace", "adal", "ada@example.com"),
        ("u-handle", "", "pfbyjy", "p@example.com"),
        ("u-email", None, None, "pfbyjy@gmail.com"),
    ]
    session = MagicMock()
    session.execute = AsyncMock(return_value=rows)

    asyncio.run(deliveries._fill_user_names(session, "org1", board))

    row = board.tasks[0]
    assert row.qa_owner_name == "Ada Lovelace"
    assert row.checks[0].checked_by_name == "Ada Lovelace"
    assert row.checks[1].checked_by_name == "pfbyjy"
    assert row.defects[0].acknowledged_by_name == "@pfbyjy"
    # No user row: the UI falls back to the id.
    assert board.delivery_checks[0].checked_by_name is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["claim", "patch"])
@pytest.mark.parametrize("has_identity", [True, False])
async def test_qa_work_requires_task_scope_and_a_user(operation, has_identity):
    from auth import APIKeyScope, AuthContext
    from auth.types import AuthMethod
    from oddish.schemas import QAWorkClaim, QAWorkPatch

    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org1",
        user_id="user1" if has_identity else None,
        scope=APIKeyScope.READ if has_identity else APIKeyScope.TASKS,
    )
    with pytest.raises(HTTPException) as exc:
        if operation == "claim":
            await deliveries.claim_qa_work("d1", QAWorkClaim(version_ids=["v1"]), auth)
        else:
            await deliveries.patch_qa_work(
                "d1", QAWorkPatch(version_id="v1", note="edit"), auth
            )
    assert exc.value.status_code == 403
