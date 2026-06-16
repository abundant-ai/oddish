import json
import urllib.error

import pytest

import oddish.cc_chat_query_cli as cli


def _run(monkeypatch, argv, fake_get):
    monkeypatch.setattr(cli, "_get", fake_get)
    out = []
    monkeypatch.setattr(cli, "_print", out.append)
    cli.main(argv)
    return "\n".join(out)


def test_search_projects_to_compact_card(monkeypatch):
    fat = {
        "items": [{
            "id": "t1", "name": "cad-bracket",
            "user_tags": [{"name": "cad"}, {"name": "mech"}],
            "total_trials": 10, "reward_success": 4, "reward_total": 8,
            "last_run_at": "2026-06-01T00:00:00Z",
            "latest_trials": [{"big": "x" * 5000}],
            "experiments": [{"id": "e1"}],
        }],
        "has_more": False,
    }
    out = _run(monkeypatch, ["tasks", "search", "--q", "cad"], lambda p, params=None: fat)
    row = json.loads(out.splitlines()[0])
    assert row == {
        "id": "t1", "name": "cad-bracket", "tags": ["cad", "mech"],
        "total_trials": 10, "pass_rate": 0.5, "last_run_at": "2026-06-01T00:00:00Z",
    }
    assert "latest_trials" not in out and "experiments" not in out


def test_logs_truncates_head_and_tail(monkeypatch):
    big = "A" * 3000 + "B" * 3000 + "C" * 3000
    out = _run(monkeypatch, ["trials", "logs", "tr1"], lambda p, params=None: big)
    assert out.startswith("A") and out.rstrip().endswith("C")
    assert "[truncated]" in out
    assert len(out) < len(big)


def test_search_maps_params(monkeypatch):
    seen = {}

    def fake_get(path, params=None):
        seen["path"] = path
        seen["params"] = params
        return {"items": [], "has_more": False}

    _run(monkeypatch, ["tasks", "search", "--q", "x", "--tags-any", "a,b", "--limit", "5"], fake_get)
    assert seen["path"] == "/tasks/browse"
    assert seen["params"]["query"] == "x"
    assert seen["params"]["tags_any"] == "a,b"
    assert seen["params"]["limit"] == 5


def test_search_truncates_when_over_budget(monkeypatch):
    items = [
        {"id": f"t{i}", "name": "x" * 500, "user_tags": [],
         "total_trials": 1, "reward_success": 0, "reward_total": 0,
         "last_run_at": None}
        for i in range(60)
    ]
    out = _run(monkeypatch, ["tasks", "search"], lambda p, params=None: {"items": items, "has_more": True})
    last = json.loads(out.splitlines()[-1])
    assert last.get("_truncated") is True


def test_get_401_emits_credential_expired(monkeypatch):
    monkeypatch.setenv("ODDISH_API_BASE_URL", "http://localhost:8800")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    out = []
    monkeypatch.setattr(cli, "_print", out.append)
    with pytest.raises(SystemExit):
        cli._get("/tasks/browse")
    payload = json.loads(out[-1])
    assert payload == {"error": "session credential expired", "status": 401}
