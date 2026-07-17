"""Per-user Slack alert preferences: which DM alerts a user gets, and at what
cutoffs.

User-session auth only, scoped to the caller -- an oddish API key must not read
or write someone's notification prefs. The response also reports the deploy-time
cutoffs so the UI can show what an unset (inherited) cutoff resolves to.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError

from auth import AuthContext, AuthMethod, require_auth
from oddish.db import get_session
from pg_errors import is_undefined_column_or_table_error
from user_alert_prefs import (
    DEFAULT_EXPERIMENT_MILESTONE_USD,
    DEFAULT_TRIAL_PING_USD,
    UserAlertPrefs,
    get_user_alert_prefs,
    set_user_alert_prefs,
)

router = APIRouter(prefix="/users/me/alert-preferences", tags=["Notifications"])


class AlertPreferences(BaseModel):
    cost_milestone_enabled: bool = True
    expensive_trial_enabled: bool = True
    experiment_failed_enabled: bool = True
    trial_failed_enabled: bool = True
    qa_failed_enabled: bool = True
    # null means "inherit the admin/global cutoff"; a value pins it for this user.
    # gt=0 also rejects NaN (NaN > 0 is false).
    experiment_milestone_usd: float | None = Field(default=None, gt=0)
    trial_ping_usd: float | None = Field(default=None, gt=0)


class AlertPreferencesResponse(AlertPreferences):
    # What an unset cutoff resolves to right now -- the UI shows these as the
    # placeholder on an empty field.
    inherited_experiment_milestone_usd: float
    inherited_trial_ping_usd: float


def _require_user_session(auth: AuthContext) -> str:
    if auth.method == AuthMethod.API_KEY:
        raise HTTPException(
            status_code=403, detail="Alert preferences require user login"
        )
    if not auth.user_id:
        raise HTTPException(
            status_code=403, detail="Alert preferences require a user identity"
        )
    return auth.user_id


def _response(prefs: UserAlertPrefs) -> AlertPreferencesResponse:
    # The inherited cutoffs are the deploy-time DM defaults -- not admin-tunable;
    # the admin pane governs only the shared-channel escalation.
    return AlertPreferencesResponse(
        **asdict(prefs),
        inherited_experiment_milestone_usd=DEFAULT_EXPERIMENT_MILESTONE_USD,
        inherited_trial_ping_usd=DEFAULT_TRIAL_PING_USD,
    )


def _unavailable(exc: ProgrammingError) -> HTTPException:
    if not is_undefined_column_or_table_error(exc):
        raise exc
    return HTTPException(
        status_code=503,
        detail="Alert preferences are not available yet (schema is still migrating).",
    )


@router.get("", response_model=AlertPreferencesResponse)
async def get_alert_preferences(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> AlertPreferencesResponse:
    user_id = _require_user_session(auth)
    try:
        async with get_session() as session:
            prefs = await get_user_alert_prefs(session, user_id)
    except ProgrammingError as exc:
        raise _unavailable(exc) from exc
    return _response(prefs)


@router.put("", response_model=AlertPreferencesResponse)
async def update_alert_preferences(
    payload: AlertPreferences,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> AlertPreferencesResponse:
    user_id = _require_user_session(auth)
    try:
        async with get_session() as session:
            prefs = await set_user_alert_prefs(
                session, user_id, UserAlertPrefs(**payload.model_dump())
            )
    except ProgrammingError as exc:
        raise _unavailable(exc) from exc
    return _response(prefs)
