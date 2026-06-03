from __future__ import annotations

import tomllib
from pathlib import Path


class TaskTimeoutValidationError(ValueError):
    """Raised when a task is missing explicit timeout settings."""


_REQUIRED_TIMEOUT_FIELDS = (
    ("agent", "timeout_sec", "[agent].timeout_sec"),
    ("environment", "build_timeout_sec", "[environment].build_timeout_sec"),
)


def validate_task_timeout_config(task_dir: Path) -> None:
    """Require explicit timeout fields in ``task.toml``.

    Oddish intentionally avoids synthesizing timeout defaults so Harbor tasks
    must declare their timeout budget directly in ``task.toml``.
    """

    config_path = task_dir / "task.toml"
    if not config_path.exists():
        raise TaskTimeoutValidationError(f"Task is missing task.toml: {config_path}")

    try:
        raw_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise TaskTimeoutValidationError(
            f"Failed to parse task.toml at {config_path}: {exc}"
        ) from exc

    missing_fields: list[str] = []
    invalid_fields: list[str] = []

    for section_name, field_name, display_name in _REQUIRED_TIMEOUT_FIELDS:
        section = raw_config.get(section_name)
        if not isinstance(section, dict) or field_name not in section:
            missing_fields.append(display_name)
            continue

        value = section[field_name]
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            invalid_fields.append(display_name)

    verifier = raw_config.get("verifier")
    verifiers = raw_config.get("verifiers")
    if isinstance(verifier, dict) and "timeout_sec" in verifier:
        value = verifier["timeout_sec"]
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            invalid_fields.append("[verifier].timeout_sec")
    elif isinstance(verifiers, list) and verifiers:
        for index, stage in enumerate(verifiers):
            display_name = f"[[verifiers]][{index}].timeout_sec"
            if not isinstance(stage, dict) or "timeout_sec" not in stage:
                missing_fields.append(display_name)
                continue
            value = stage["timeout_sec"]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or value <= 0
            ):
                invalid_fields.append(display_name)
    else:
        missing_fields.append("[verifier].timeout_sec or [[verifiers]].timeout_sec")

    if missing_fields or invalid_fields:
        details: list[str] = []
        if missing_fields:
            details.append(f"missing: {', '.join(missing_fields)}")
        if invalid_fields:
            details.append(
                "must be positive numbers: " + ", ".join(sorted(invalid_fields))
            )
        raise TaskTimeoutValidationError(
            "Oddish requires task-defined timeouts in task.toml; "
            + "; ".join(details)
            + "."
        )
