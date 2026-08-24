"""A stable-variant trial's recorded pin is refreshed the moment it is claimed.

The deployment is the unit of harbor identity for a stable variant, so a trial
queued across a pin bump executes the new deployment's harbor. The claim path
must rewrite the recorded pin so the row matches execution; ephemeral
(exact-pin) trials run the sha they recorded and must be left alone.
"""

from types import SimpleNamespace

from oddish.core.harbor_source import GKE_VARIANT_ID, HARBOR_VARIANTS
from oddish.workers.queue.trial_handler import _refresh_stable_variant_pin

_BLESSED = HARBOR_VARIANTS[GKE_VARIANT_ID]


def _trial(harbor_config):
    return SimpleNamespace(id="t-1", harbor_config=harbor_config, harbor_sha=None)


def test_a_stale_stable_pin_is_rewritten_to_the_deployed_one():
    trial = _trial(
        {
            "variant_id": GKE_VARIANT_ID,
            "source": _BLESSED.source,
            "resolved_sha": "0" * 40,
            "mode": "run",
        }
    )
    refreshed = _refresh_stable_variant_pin(trial)
    assert refreshed["resolved_sha"] == _BLESSED.sha
    assert refreshed["source"] == _BLESSED.source
    assert refreshed["mode"] == "run"
    assert trial.harbor_config is refreshed
    assert trial.harbor_sha == _BLESSED.sha


def test_a_current_stable_pin_is_left_untouched():
    config = {
        "variant_id": GKE_VARIANT_ID,
        "source": _BLESSED.source,
        "resolved_sha": _BLESSED.sha,
    }
    trial = _trial(config)
    assert _refresh_stable_variant_pin(trial) is config
    assert trial.harbor_sha is None


def test_an_ephemeral_exact_pin_is_never_rewritten():
    config = {
        "variant_id": "ephemeral",
        "source": "https://github.com/example/harbor-fork",
        "resolved_sha": "1" * 40,
    }
    trial = _trial(config)
    assert _refresh_stable_variant_pin(trial) is config
    assert trial.harbor_config["resolved_sha"] == "1" * 40
    assert trial.harbor_sha is None


def test_a_missing_or_malformed_config_is_left_alone():
    assert _refresh_stable_variant_pin(_trial(None)) is None
    trial = _trial("not-a-dict")
    assert _refresh_stable_variant_pin(trial) == "not-a-dict"
