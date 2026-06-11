from types import SimpleNamespace
from oddish.worker.probe_overlay import select_related_trials


def _t(tid, mode=None):
    return SimpleNamespace(id=tid, harbor_config={"mode": mode} if mode else {})


def test_trial_scope_selects_only_target():
    trials = [_t("a-0"), _t("a-1"), _t("a-2")]
    out = select_related_trials(
        trials, current_trial_id="a-3", target_trial_id="a-1"
    )
    assert [t.id for t in out] == ["a-1"]


def test_target_excludes_current_even_if_equal():
    trials = [_t("a-1")]
    out = select_related_trials(
        trials, current_trial_id="a-1", target_trial_id="a-1"
    )
    assert out == []


def test_no_target_keeps_existing_sibling_behavior():
    trials = [_t("a-0"), _t("a-1", mode="probe")]
    out = select_related_trials(trials, current_trial_id="a-9")
    assert [t.id for t in out] == ["a-0"]


def test_excludes_by_is_probe_flag_when_present():
    trials = [
        SimpleNamespace(id="a-0", harbor_config={}, is_probe=True),
        SimpleNamespace(id="a-1", harbor_config={}, is_probe=False),
    ]
    out = select_related_trials(trials, current_trial_id="a-9")
    assert [t.id for t in out] == ["a-1"]
