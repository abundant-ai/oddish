from __future__ import annotations

import os

from fastapi import HTTPException

from auth import AuthContext


def is_operator_org(auth: AuthContext) -> bool:
    operator_org_id = os.environ.get("ODDISH_OPERATOR_ORG_ID", "").strip()
    return bool(operator_org_id and auth.org_id == operator_org_id)


def require_operator_org(auth: AuthContext) -> None:
    if not is_operator_org(auth):
        raise HTTPException(status_code=403, detail="Operator organization required")
