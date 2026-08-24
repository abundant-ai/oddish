"""A stable-variant trial's recorded pin is refreshed the moment it is claimed.

The deployment is the unit of harbor identity for stable variants: the gke
variant image bakes the blessed gke pin, every other worker executes the
locked default pin, and a trial queued across a pin bump executes whatever
the deployment now ships. The claim path must rewrite the recorded pin so the
row matches execution, and must reconcile the indexed projection even for a
matching pin (retry/combine/import copies persist harbor_config without it).
Ephemeral exact-pin trials run the sha they recorded; configs with no harbor
identity at all execute no pin and are untouched.
"""

from types import SimpleNamespace

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE
from oddish.core.harbor_source import GKE_VARIANT_ID, HARBOR_VARIANTS
from oddish.workers.queue.trial_handler import _refresh_stable_variant_pin

_BLESSED = HARBOR_VARIANTS[GKE_VARIANT_ID]


def _trial(harbor_config, harbor_sha=None):
    return SimpleNamespace(id="t-1", harbor_config=harbor_config, harbor_sha=harbor_sha)


def test_a_stale_gke_pin_is_rewritten_to_the_deployed_one():
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


def test_a_stale_default_pin_is_rewritten_to_the_locked_one():
    """Every non-registered variant executes the locked default pin, so a
    'default' (or absent) variant id must be refreshed the same way -- the
    default worker image is just as deploy-locked as the gke image."""
    trial = _trial(
        {
            "variant_id": "default",
            "source": HARBOR_DEFAULT_SOURCE,
            "resolved_sha": "0" * 40,
        }
    )
    refreshed = _refresh_stable_variant_pin(trial)
    assert refreshed["resolved_sha"] == HARBOR_DEFAULT_SHA
    assert refreshed["source"] == HARBOR_DEFAULT_SOURCE
    assert trial.harbor_sha == HARBOR_DEFAULT_SHA


def test_a_matching_pin_still_reconciles_the_projection():
    """Immutable retry, combine, and import persist harbor_config without the
    indexed projection; sha filters query only the projection, so the claim
    must heal it even when the pin itself is current."""
    config = {
        "variant_id": GKE_VARIANT_ID,
        "source": _BLESSED.source,
        "resolved_sha": _BLESSED.sha,
    }
    trial = _trial(config, harbor_sha=None)
    assert _refresh_stable_variant_pin(trial) is config
    assert trial.harbor_sha == _BLESSED.sha


def test_an_ephemeral_exact_pin_keeps_its_sha_and_syncs_the_projection():
    config = {
        "variant_id": "ephemeral",
        "source": "https://github.com/example/harbor-fork",
        "resolved_sha": "1" * 40,
    }
    trial = _trial(config)
    assert _refresh_stable_variant_pin(trial) is config
    assert trial.harbor_config["resolved_sha"] == "1" * 40
    assert trial.harbor_sha == "1" * 40


def test_a_config_with_no_harbor_identity_is_left_alone():
    """Audit/analysis payloads execute no pin; stamping one would invent
    identity for a row that never ran harbor."""
    config = {"mode": "audit", "analysis_payload": {"k": 1}}
    trial = _trial(config)
    assert _refresh_stable_variant_pin(trial) is config
    assert "resolved_sha" not in trial.harbor_config
    assert trial.harbor_sha is None


def test_local_mode_stamps_what_it_executes_not_the_blessed_pin():
    """Local mode executes the installed default harbor even for a trial
    labelled with a registered variant; stamping the blessed variant pin
    there would recreate the record-vs-execution skew this module cures."""
    trial = _trial(
        {
            "variant_id": GKE_VARIANT_ID,
            "source": _BLESSED.source,
            "resolved_sha": _BLESSED.sha,
        }
    )
    refreshed = _refresh_stable_variant_pin(
        trial, executing=(HARBOR_DEFAULT_SOURCE, HARBOR_DEFAULT_SHA)
    )
    assert refreshed["resolved_sha"] == HARBOR_DEFAULT_SHA
    assert refreshed["source"] == HARBOR_DEFAULT_SOURCE
    assert trial.harbor_sha == HARBOR_DEFAULT_SHA


def test_a_missing_or_malformed_config_is_left_alone():
    assert _refresh_stable_variant_pin(_trial(None)) is None
    trial = _trial("not-a-dict")
    assert _refresh_stable_variant_pin(trial) == "not-a-dict"


def test_the_claim_path_actually_invokes_the_refresh(monkeypatch):
    """The helper being correct is worthless if the claim block stops calling
    it: pin the wiring, not just the logic."""
    import oddish.workers.queue.trial_handler as th

    calls = []

    def _spy(trial):
        calls.append(trial)
        return {"spied": True}

    monkeypatch.setattr(th, "_refresh_stable_variant_pin", _spy)
    src = (
        th._prepare_trial_run.__wrapped__
        if hasattr(th._prepare_trial_run, "__wrapped__")
        else th._prepare_trial_run
    )
    import inspect

    assert "_refresh_stable_variant_pin(trial)" in inspect.getsource(src), (
        "the claim/prepare path no longer routes trial harbor_config through "
        "_refresh_stable_variant_pin; worker-runtime invariant 7 is broken"
    )
