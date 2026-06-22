from oddish.cli.api import build_sweep_payload
from oddish.config import resolve_harbor_layers


def test_resolve_then_build_sweep_payload_carries_source_and_ref():
    source, ref = resolve_harbor_layers(
        flag="dot-agi/harbor@feature/x", env=None, manifest=None
    )
    payload = build_sweep_payload(
        task_id="task",
        configs=[{"agent": "nop", "n_trials": 1}],
        environment=None,
        user=None,
        priority="low",
        experiment_id=None,
        harbor_config={"source": source, "ref": ref},
    )
    assert payload["harbor"]["source"] == "https://github.com/dot-agi/harbor"
    assert payload["harbor"]["ref"] == "feature/x"


def test_no_override_leaves_harbor_payload_clean():
    payload = build_sweep_payload(
        task_id="task",
        configs=[{"agent": "nop", "n_trials": 1}],
        environment=None,
        user=None,
        priority="low",
        experiment_id=None,
        harbor_config=None,
    )
    assert "harbor" not in payload or "source" not in payload.get("harbor", {})
