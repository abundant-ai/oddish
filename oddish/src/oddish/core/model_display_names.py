"""Per-experiment operator model aliases, applied to published share responses.

``apply_model_display_names`` rewrites already-built ``TrialResponse`` objects,
after ``build_trial_response`` resolved cost from the real model id. Nothing may
run it earlier or feed an alias into a pricing lookup -- those key off
``trials.model`` and would mis-price.
"""

from collections.abc import Iterable

from oddish.config import normalize_model_id
from oddish.schemas import TrialResponse


def _lookup_keys(model: str | None) -> list[str]:
    if not model:
        return []
    keys = (model.strip().lower(), normalize_model_id(model))
    return list(dict.fromkeys(key for key in keys if key))


def canonical_model_key(model: str) -> str:
    """The single spelling an alias is stored under; ``_lookup_keys`` covers the
    same case/whitespace variants on the read side, so writes and reads meet."""
    keys = _lookup_keys(model)
    return keys[-1] if keys else ""


def experiment_display_names(experiment) -> dict[str, str]:
    """One experiment's stored alias map, each key expanded through
    ``_lookup_keys`` so reads match whatever spelling a trial carries."""
    stored = getattr(experiment, "public_model_renames", None) or {}
    names: dict[str, str] = {}
    for model_name, display in stored.items():
        if not display:
            continue
        for key in _lookup_keys(model_name):
            names.setdefault(key, display)
    return names


def display_model_name(model: str | None, names: dict[str, str]) -> str | None:
    """The alias for one model id, or the id unchanged when none is set."""
    if not names or not model:
        return model
    return next((names[key] for key in _lookup_keys(model) if key in names), model)


def mask_trajectory_model_names(
    trajectory: dict | None, names: dict[str, str]
) -> dict | None:
    """Rewrite the model ids a share page renders from an ATIF trajectory's own
    ``agent.model_name`` and per-step ``model_name``.

    Copies rather than mutates: ``read_trial_trajectory`` memoizes the parsed
    document, and the authenticated route serves that same object, which must
    keep the real ids.
    """
    if not names or not trajectory:
        return trajectory

    def masked_name(value):
        # Agent-written JSON: a non-string model_name would 500 in _lookup_keys.
        if not isinstance(value, str):
            return value
        return display_model_name(value, names)

    masked = dict(trajectory)
    agent = masked.get("agent")
    if isinstance(agent, dict):
        masked["agent"] = {**agent, "model_name": masked_name(agent.get("model_name"))}
    steps = masked.get("steps")
    if isinstance(steps, list):
        masked["steps"] = [
            {**step, "model_name": masked_name(step.get("model_name"))}
            if isinstance(step, dict)
            else step
            for step in steps
        ]
    return masked


def apply_model_display_names(
    trials: Iterable[TrialResponse], names: dict[str, str]
) -> None:
    if not names:
        return
    for trial in trials:
        keys = _lookup_keys(trial.model)
        display = next((names[key] for key in keys if key in names), None)
        if display is None:
            continue
        queue_key = trial.queue_key or ""
        prefix, sep, last = queue_key.rpartition("/")
        if queue_key.strip().lower() in keys:
            trial.queue_key = display
        elif sep and last.strip().lower() in keys:
            trial.queue_key = f"{prefix}{sep}{display}"
        trial.model = display
