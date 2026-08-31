from __future__ import annotations

from fastapi import HTTPException, Request, status

from auth.types import AuthContext
from oddish.core.analysis_payload import (
    AnalysisPayloadError,
    analysis_source_trial_ids,
)
from oddish.db import TaskVersionModel, TrialModel, get_session


_QA_TRIAL_READ_ROUTES = frozenset(
    {
        "/trials/{trial_id}/result",
        "/trials/{trial_id}/trajectory",
        "/trials/{trial_id}/logs",
        "/trials/{trial_id}/logs/structured",
    }
)


def _denied() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This analysis credential is not authorized for the requested resource",
    )


async def authorize_bound_analysis_request(request: Request, auth: AuthContext) -> None:
    """Restrict an analysis sandbox key to resources derived from its Trial row."""
    analysis_trial_id = auth.bound_analysis_trial_id
    if analysis_trial_id is None:
        return
    if request.method != "GET":
        raise _denied()

    route = request.scope.get("route")
    route_path = getattr(route, "path", "")
    path_params = request.path_params

    async with get_session() as session:
        analysis_trial = await session.get(TrialModel, analysis_trial_id)
        if analysis_trial is None or analysis_trial.org_id != auth.org_id:
            raise _denied()

        target_trial_id = path_params.get("trial_id")
        if route_path in _QA_TRIAL_READ_ROUTES and target_trial_id:
            if analysis_trial.kind not in ("qa", "qa_eval"):
                raise _denied()
            try:
                trial_ids = analysis_source_trial_ids(
                    analysis_trial.kind,
                    analysis_trial.harbor_config,
                )
            except AnalysisPayloadError as exc:
                raise _denied() from exc
            if target_trial_id not in trial_ids:
                raise _denied()
            return

        task_id = path_params.get("task_id")
        if (
            route_path
            in (
                "/tasks/{task_id}/files",
                "/tasks/{task_id}/files/{file_path:path}",
            )
            and task_id
        ):
            if analysis_trial.kind != "audit" or analysis_trial.task_id != task_id:
                raise _denied()
            if analysis_trial.task_version_id is None:
                raise _denied()
            pinned_version = await session.get(
                TaskVersionModel, analysis_trial.task_version_id
            )
            requested_version = request.query_params.get("version")
            if (
                pinned_version is None
                or pinned_version.task_id != task_id
                or requested_version is None
                or requested_version != str(pinned_version.version)
            ):
                raise _denied()
            return

    raise _denied()
