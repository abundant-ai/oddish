from __future__ import annotations

import inspect

from fastapi.routing import APIRoute

from api.app import create_app
from api.routers.tasks import unpublish_experiment
from auth import require_admin, require_auth


PRIVATE_ROUTES = {
    ("GET", "/experiments/{experiment_id}/qa"): require_auth,
    ("POST", "/experiments/{experiment_id}/qa"): require_admin,
    ("PATCH", "/experiments/{experiment_id}/qa"): require_admin,
    ("POST", "/experiments/{experiment_id}/qa/sync"): require_admin,
    ("GET", "/experiments/{experiment_id}/qa/preview"): require_auth,
    ("POST", "/experiments/{experiment_id}/qa/publish"): require_admin,
    ("POST", "/experiments/{experiment_id}/qa/unpublish"): require_admin,
}


def _route_map() -> dict[tuple[str, str], APIRoute]:
    routes: dict[tuple[str, str], APIRoute] = {}
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            routes[(method, route.path)] = route
    return routes


def test_qa_report_routes_and_auth_dependencies_are_registered() -> None:
    routes = _route_map()
    for key, dependency in PRIVATE_ROUTES.items():
        route = routes[key]
        dependency_calls = {row.call for row in route.dependant.dependencies}
        assert dependency in dependency_calls

    public = routes[("GET", "/public/experiments/{public_token}/qa/{qa_token}")]
    dependency_calls = {row.call for row in public.dependant.dependencies}
    assert require_auth not in dependency_calls
    assert require_admin not in dependency_calls


def test_mutations_require_reviewed_draft_version_body() -> None:
    routes = _route_map()
    for key in (
        ("PATCH", "/experiments/{experiment_id}/qa"),
        ("POST", "/experiments/{experiment_id}/qa/publish"),
        ("POST", "/experiments/{experiment_id}/qa/unpublish"),
    ):
        route = routes[key]
        assert route.body_field is not None
        field = route.body_field.type_.model_fields["expected_draft_version"]
        assert field.is_required()
    for key in (
        ("POST", "/experiments/{experiment_id}/qa/publish"),
        ("POST", "/experiments/{experiment_id}/qa/unpublish"),
    ):
        route = routes[key]
        assert route.body_field is not None
        token_field = route.body_field.type_.model_fields["expected_public_token"]
        assert token_field.is_required()


def test_experiment_unpublish_revokes_qa_link() -> None:
    source = inspect.getsource(unpublish_experiment)
    assert "revoke_public_qa_report_core(" in source
    assert source.index("revoke_public_qa_report_core(") < source.index(
        "experiment.public_token = None"
    )
