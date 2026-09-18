"""``oddish delivery inventory`` against a fake API."""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from oddish.cli.delivery import delivery_app

INVENTORY = {
    "schema_version": "oddish-task-inventory-v1",
    "org_id": "org-1",
    "tasks": [],
}


class _Resp:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


def _run(args: list[str], responses: dict[tuple[str, str], object]):
    """``responses`` maps (method, path) to a payload; every call is recorded."""
    calls: list[dict] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def request(self, method, url, **kwargs):
            path = url.replace("http://api", "")
            files = kwargs.get("files") or {}
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "data": kwargs.get("data"),
                    "params": kwargs.get("params"),
                    "files": {k: v[0] for k, v in files.items()},
                }
            )
            return _Resp(200, responses[(method, path)])

    with (
        patch("oddish.cli.delivery.httpx.Client", _Client),
        patch("oddish.cli.delivery.get_api_url", return_value="http://api"),
        patch("oddish.cli.delivery.get_auth_headers", return_value={}),
    ):
        result = CliRunner().invoke(delivery_app, args)
    return result, calls


def test_inventory_writes_a_new_file_and_refuses_to_overwrite(tmp_path):
    output = tmp_path / "inventory.json"
    result, calls = _run(
        ["inventory", "--output", str(output)],
        {("GET", "/deliveries/task-inventory"): INVENTORY},
    )
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text()) == INVENTORY
    assert "0 task identities for organization org-1" in result.output
    result, calls = _run(["inventory", "--output", str(output)], {})
    assert result.exit_code == 1 and "already exists" in result.output
    assert calls == []
