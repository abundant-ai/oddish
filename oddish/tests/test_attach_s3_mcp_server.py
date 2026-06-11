from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSubmission, TrialSpec


def _sub(**kw):
    return TaskSubmission(
        task_path="t", trials=[TrialSpec(agent="nop", model=None)], **kw
    )


def test_task_scope_attaches_mcp_server(monkeypatch):
    monkeypatch.setattr("oddish.queue.settings.mcp_s3_base_url", "https://mcp.test")
    monkeypatch.setattr("oddish.queue.settings.mcp_s3_signing_key", "k")
    monkeypatch.setattr("oddish.queue._mcp_now", lambda: 1000)
    cfg = _build_harbor_config_for_trial(
        _sub(extra_instructions="poke", probe_scope="task"),
        TrialSpec(agent="nop", model=None),
        experiment_id="exp1",
    )
    servers = cfg["mcp_servers"]
    assert servers[0]["name"] == "oddish-s3"
    assert servers[0]["transport"] == "streamable-http"
    assert "token=" in servers[0]["url"]
    assert servers[0]["url"].startswith("https://mcp.test/mcp?token=")


def test_trial_scope_has_no_mcp_server():
    cfg = _build_harbor_config_for_trial(
        _sub(extra_instructions="poke", probe_scope="trial",
             probe_target_trial_id="t-0"),
        TrialSpec(agent="nop", model=None),
        experiment_id="exp1",
    )
    assert "mcp_servers" not in cfg


def test_task_scope_no_mcp_when_unconfigured(monkeypatch):
    # No base url / key configured (e.g. local dev) -> probe still builds, no MCP.
    monkeypatch.setattr("oddish.queue.settings.mcp_s3_base_url", "")
    monkeypatch.setattr("oddish.queue.settings.mcp_s3_signing_key", "")
    cfg = _build_harbor_config_for_trial(
        _sub(extra_instructions="poke", probe_scope="task"),
        TrialSpec(agent="nop", model=None),
        experiment_id="exp1",
    )
    assert "mcp_servers" not in cfg
