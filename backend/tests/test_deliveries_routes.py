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
