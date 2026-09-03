from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from auth import require_admin

_ROUTER_PATH = (
    Path(__file__).resolve().parents[1] / "api" / "routers" / "deliveries.py"
)
_SPEC = spec_from_file_location("deliveries_route_under_test", _ROUTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
deliveries = module_from_spec(_SPEC)
_SPEC.loader.exec_module(deliveries)


def test_every_mutation_requires_admin() -> None:
    """POST/PATCH/PUT/DELETE delivery routes must depend on require_admin."""
    mutations = 0
    for route in deliveries.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if not (methods - {"GET", "HEAD", "OPTIONS"}):
            continue
        mutations += 1
        dependant = getattr(route, "dependant", None)
        assert dependant is not None
        calls = [d.call for d in dependant.dependencies]
        assert require_admin in calls, (
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
    assert row.checks[0].checked_by_name == "Ada Lovelace"
    assert row.checks[1].checked_by_name == "pfbyjy"
    assert row.defects[0].acknowledged_by_name == "@pfbyjy"
    # No user row: the UI falls back to the id.
    assert board.delivery_checks[0].checked_by_name is None
