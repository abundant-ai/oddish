from dataclasses import dataclass, field

from oddish.worker.probe_overlay import select_experiment_related_trials


@dataclass
class T:
    id: str
    task_id: str
    harbor_config: dict = field(default_factory=dict)


def test_excludes_self_and_probes():
    trials = [
        T("self", "a"),
        T("probe", "a", {"mode": "probe"}),
        T("real", "a"),
    ]
    out = select_experiment_related_trials(trials, current_trial_id="self")
    assert [t.id for t in out] == ["real"]


def test_balances_across_tasks_under_cap():
    # task "a" is noisy (5), task "b" has 1. Cap=4 must not let "a" crowd "b" out.
    trials = [T(f"a{i}", "a") for i in range(5)] + [T("b0", "b")]
    out = select_experiment_related_trials(trials, current_trial_id="x", cap=4)
    ids = {t.id for t in out}
    assert "b0" in ids
    assert len(out) == 4


def test_respects_cap():
    trials = [T(f"a{i}", "a") for i in range(10)]
    out = select_experiment_related_trials(trials, current_trial_id="x", cap=3)
    assert len(out) == 3


def test_no_trials_returns_empty():
    assert select_experiment_related_trials([], current_trial_id="x") == []
