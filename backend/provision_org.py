"""Operator-only CLI: approve an exact Clerk org, or revoke and stop its work.

Run against the intended deployment database with its matching Clerk secret.
This module is not exposed as an HTTP endpoint or to organization admins.
"""

import argparse
import asyncio
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from auth.provisioning import fetch_and_sync_clerk_org
from models import OrganizationModel, OrgQuotaModel, generate_id
from oddish.db import get_session, utcnow


async def provision(
    clerk_org_id: str, *, enable: bool, monthly_limit: Decimal | None
) -> str:
    async with get_session() as session:
        if enable:
            org = await fetch_and_sync_clerk_org(session, clerk_org_id)
            if not org.is_active or org.deleted_at is not None:
                raise ValueError("Cannot approve a deactivated organization")
            if (
                monthly_limit is None
                or not monthly_limit.is_finite()
                or monthly_limit <= 0
            ):
                raise ValueError(
                    "Approval requires a positive, finite monthly USD limit"
                )
            await session.execute(
                insert(OrgQuotaModel)
                .values(
                    id=generate_id(),
                    org_id=org.id,
                    limit_usd=monthly_limit,
                    period_kind="monthly",
                )
                .on_conflict_do_update(
                    index_elements=["org_id"],
                    index_where=OrgQuotaModel.deleted_at.is_(None),
                    set_={"limit_usd": monthly_limit, "updated_at": utcnow()},
                )
            )
        else:
            org = await session.scalar(
                select(OrganizationModel).where(
                    OrganizationModel.clerk_org_id == clerk_org_id
                )
            )
            if org is None:
                raise ValueError("Organization is not provisioned in this database")
        org.execution_enabled = enable
        await session.commit()
        org_id = org.id
    if not enable:
        from worker.org_access import cancel_unapproved_runs

        while await cancel_unapproved_runs():
            pass
    return org_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clerk-org-id", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--approve", action="store_true")
    action.add_argument("--revoke", action="store_true")
    parser.add_argument("--monthly-limit-usd", type=Decimal)
    args = parser.parse_args()
    org_id = asyncio.run(
        provision(
            args.clerk_org_id, enable=args.approve, monthly_limit=args.monthly_limit_usd
        )
    )
    print(f"organization={org_id} execution_enabled={args.approve}")


if __name__ == "__main__":
    main()
