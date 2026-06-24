from pathlib import Path

from oddish.cli.api import build_sweep_payload
from oddish.cli.run import _read_harbor_manifest
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


def test_manifest_reads_oddish_toml_harbor_table(tmp_path: Path):
    task_dir = tmp_path / "mytask"
    task_dir.mkdir()
    (task_dir / "oddish.toml").write_text(
        '[harbor]\nsource = "https://github.com/dot-agi/harbor"\nref = "v9@rc1"\n'
    )
    manifest = _read_harbor_manifest(task_dir, None)
    assert manifest == {
        "source": "https://github.com/dot-agi/harbor",
        "ref": "v9@rc1",
    }
    # And the precedence resolver consumes it (manifest layer, literal-'@' kept).
    assert resolve_harbor_layers(flag=None, env=None, manifest=manifest) == (
        "https://github.com/dot-agi/harbor",
        "v9@rc1",
    )


def test_manifest_absent_returns_none(tmp_path: Path):
    task_dir = tmp_path / "no-sidecar"
    task_dir.mkdir()
    assert _read_harbor_manifest(task_dir, None) is None
