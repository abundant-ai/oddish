from __future__ import annotations

import json
import importlib

from harbor.models.task.config import TaskConfig
import pytest
from typer.testing import CliRunner

from oddish.cli import app
from oddish.preflight.checks import task_metadata
from oddish.preflight.runner import run_checks


def _findings(task_dir):
    config = TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())
    return task_metadata.check(task_dir, config)


@pytest.mark.parametrize(
    "name", ["abundant/sample-task", "category/sample-task", "sample-task"]
)
def test_matching_names_pass_through_cli(make_task, name):
    task_dir = make_task(extra_files={"environment/.dockerignore": "**/.git\n"})
    path = task_dir / "task.toml"
    path.write_text(path.read_text().replace("abundant/sample-task", name))
    result = CliRunner().invoke(app, ["preflight", str(task_dir), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"ok": True, "findings": []}


@pytest.mark.parametrize("declaration", ["", '[task]\nname = "abundant/other-task"\n'])
def test_name_is_explicit_and_matches_directory(make_task, declaration):
    task_dir = make_task()
    path = task_dir / "task.toml"
    path.write_text(
        path.read_text().replace('[task]\nname = "abundant/sample-task"\n', declaration)
    )
    findings = _findings(task_dir)
    assert len(findings) == 1
    assert "name" in findings[0].message
    assert findings[0].path == path


@pytest.mark.parametrize("reward", [None, '""', '"   "', "true", "42", "[]"])
def test_reward_type_requires_nonempty_string(make_task, reward):
    task_dir = make_task()
    path = task_dir / "task.toml"
    declaration = "" if reward is None else f"reward_type = {reward}"
    path.write_text(path.read_text().replace('reward_type = "binary"', declaration))
    findings = _findings(task_dir)
    assert len(findings) == 1
    assert "reward_type" in findings[0].message


def test_reward_vocabulary_is_not_restricted(make_task):
    task_dir = make_task()
    path = task_dir / "task.toml"
    path.write_text(
        path.read_text().replace(
            'reward_type = "binary"', 'reward_type = "custom multi-dimensional score"'
        )
    )
    assert _findings(task_dir) == []


@pytest.mark.parametrize(
    "network",
    [
        'network_mode = "no-network"',
        'network_mode = "allowlist"',
        'network_mode = "public"',
        "allow_internet = false",
        "allow_internet = true",
    ],
)
def test_explicit_network_declarations_are_accepted(make_task, network):
    task_dir = make_task()
    path = task_dir / "task.toml"
    path.write_text(path.read_text().replace('network_mode = "no-network"', network))
    assert _findings(task_dir) == []


def test_justified_implicit_public_still_needs_declaration(make_task):
    task_dir = make_task()
    path = task_dir / "task.toml"
    path.write_text(
        path.read_text()
        .replace('network_mode = "no-network"', "")
        .replace(
            "[metadata]",
            '[metadata]\nopen_internet_justification = "Needs public package registries for the task."',
        )
    )
    findings = run_checks([task_dir])
    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) == 1
    assert errors[0].check_id == "task_metadata"
    assert "internet-access" in errors[0].message


def test_public_declaration_does_not_bypass_existing_justification_check(make_task):
    task_dir = make_task()
    path = task_dir / "task.toml"
    path.write_text(
        path.read_text().replace(
            'network_mode = "no-network"', 'network_mode = "public"'
        )
    )
    assert _findings(task_dir) == []
    assert any(f.check_id == "closed_internet" for f in run_checks([task_dir]))


@pytest.mark.parametrize(
    "network, fails",
    [
        ('allow_internet = "false"', True),
        ('allow_internet = true\nnetwork_mode = "no-network"', False),
    ],
)
def test_legacy_boolean_and_modern_precedence(make_task, network, fails):
    task_dir = make_task()
    path = task_dir / "task.toml"
    path.write_text(path.read_text().replace('network_mode = "no-network"', network))
    assert bool(_findings(task_dir)) is fails


@pytest.mark.parametrize("prefix", ["verifier", "steps.verifier"])
def test_separate_verifier_requires_own_network_declaration(make_task, prefix):
    task_dir = make_task()
    path = task_dir / "task.toml"
    extra = '\n[[steps]]\nname = "first"\n' if prefix.startswith("steps") else "\n"
    extra += f'[{prefix}.environment]\ndocker_image = "ubuntu:24.04"\n'
    path.write_text(path.read_text() + extra)
    findings = _findings(task_dir)
    assert len(findings) == 1
    assert "verifier.environment" in findings[0].message
    path.write_text(path.read_text() + 'network_mode = "no-network"\n')
    assert _findings(task_dir) == []


@pytest.mark.parametrize(
    "gpu_types", [None, "[]", '["H100"]', '["custom-provider-gpu"]']
)
def test_gpu_types_optional_and_provider_independent(make_task, gpu_types):
    task_dir = make_task()
    if gpu_types is not None:
        path = task_dir / "task.toml"
        path.write_text(path.read_text() + f"gpu_types = {gpu_types}\n")
    assert _findings(task_dir) == []


@pytest.mark.parametrize("gpu_types", ['[""]', '["H100", "   "]'])
def test_blank_gpu_type_is_rejected(make_task, gpu_types):
    task_dir = make_task()
    path = task_dir / "task.toml"
    path.write_text(path.read_text() + f"gpu_types = {gpu_types}\n")
    findings = _findings(task_dir)
    assert len(findings) == 1
    assert "gpu_types" in findings[0].message


@pytest.mark.parametrize("command", ["run", "upload"])
@pytest.mark.parametrize("force", [False, True])
def test_metadata_gate_preserves_forced_submission(
    make_task, monkeypatch, command, force
):
    task_dir = make_task()
    path = task_dir / "task.toml"
    path.write_text(path.read_text().replace('reward_type = "binary"', ""))
    module = importlib.import_module(f"oddish.cli.{command}")
    monkeypatch.setattr(module, "get_api_url", lambda *a, **k: "http://x")
    monkeypatch.setattr(module, "require_api_key", lambda *a, **k: "key")
    uploaded = []

    def upload(*a, **k):
        uploaded.append(True)
        raise RuntimeError("reached upload")

    monkeypatch.setattr(module, "upload_tasks_with_progress", upload)
    args = [command, str(task_dir)]
    if command == "run":
        args += ["--agent", "codex"]
    if force:
        args += ["--force"]
    result = CliRunner().invoke(app, args)
    assert "reward_type" in result.output
    assert bool(uploaded) is force
    if force:
        assert "reached upload" in str(result.exception)
    else:
        assert result.exit_code == 1


@pytest.mark.parametrize(
    "declaration", ['gpu_types = "H100"', "gpu_types = [1]", 'network_mode = "invalid"']
)
def test_invalid_harbor_config_still_stops_before_metadata_checks(
    make_task, declaration
):
    task_dir = make_task()
    path = task_dir / "task.toml"
    path.write_text(
        path.read_text().replace('network_mode = "no-network"', declaration)
    )
    findings = run_checks([task_dir])
    assert len(findings) == 1
    assert findings[0].check_id == "task_config"
