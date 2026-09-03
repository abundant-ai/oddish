"""API containers are pinned next to the database unless a deploy opts out.

``ODDISH_MODAL_API_REGIONS`` is read into ``modal_app.API_REGIONS`` and passed
as ``region=`` on the API function in ``endpoints.py``. The default keeps every
API container in the two Modal regions adjacent to AWS us-east-2, where the
Supabase pooler and storage live, so the 10-20 sequential round trips a request
makes cost 5-25 ms each instead of 50-220 ms. An empty value removes the pin.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import modal_app


def _reload_with(monkeypatch, value: str | None):
    if value is None:
        monkeypatch.delenv("ODDISH_MODAL_API_REGIONS", raising=False)
    else:
        monkeypatch.setenv("ODDISH_MODAL_API_REGIONS", value)
    importlib.reload(modal_app)
    return modal_app.API_REGIONS


def test_default_pins_api_containers_next_to_the_database(monkeypatch):
    try:
        assert _reload_with(monkeypatch, None) == ("us-east", "us-central")
    finally:
        monkeypatch.undo()
        importlib.reload(modal_app)


def test_deploy_can_narrow_or_widen_the_pin(monkeypatch):
    try:
        assert _reload_with(monkeypatch, "us-east") == ("us-east",)
        assert _reload_with(monkeypatch, " us-east , us-west ,") == (
            "us-east",
            "us-west",
        )
    finally:
        monkeypatch.undo()
        importlib.reload(modal_app)


def test_blank_value_removes_the_pin(monkeypatch):
    try:
        assert _reload_with(monkeypatch, "") is None
        assert _reload_with(monkeypatch, " , ") is None
    finally:
        monkeypatch.undo()
        importlib.reload(modal_app)


def test_api_function_is_declared_with_the_pin():
    # endpoints.py builds the whole ASGI app at import, so inspect the source
    # instead of importing it.
    source = Path(modal_app.__file__).with_name("endpoints.py").read_text()
    module = ast.parse(source)
    api_functions = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "api_app"
    ]
    assert len(api_functions) == 1
    function_decorators = [
        decorator
        for decorator in api_functions[0].decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "function"
    ]
    assert len(function_decorators) == 1
    region = {kw.arg: kw.value for kw in function_decorators[0].keywords}["region"]
    assert isinstance(region, ast.Name) and region.id == "API_REGIONS"
