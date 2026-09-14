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


def test_a_stale_gke_pin_is_rewritten_to_what_the_gke_image_executes():
    """Inside the gke variant image the installed harbor IS the blessed pin;
    simulate that runtime with the explicit executing descriptor."""
    trial = _trial(
        {
            "variant_id": GKE_VARIANT_ID,
            "source": _BLESSED.source,
            "resolved_sha": "0" * 40,
            "mode": "run",
        }
    )
    refreshed = _refresh_stable_variant_pin(
        trial, executing=(_BLESSED.source, _BLESSED.sha)
    )
    assert refreshed["resolved_sha"] == _BLESSED.sha
    assert refreshed["source"] == _BLESSED.source
    assert refreshed["mode"] == "run"
    assert trial.harbor_config is refreshed
    assert trial.harbor_sha == _BLESSED.sha


def test_the_default_resolution_is_the_installed_harbor(monkeypatch):
    """When no executing descriptor is given, the refresh asks the runtime
    itself -- the imported harbor's installation metadata -- which is what
    every worker without a variant image actually executes."""
    import oddish.workers.queue.trial_handler as th

    monkeypatch.setattr(
        th, "_installed_harbor_descriptor", lambda: ("https://x/installed", "i" * 40)
    )
    trial = _trial(
        {
            "variant_id": GKE_VARIANT_ID,
            "source": _BLESSED.source,
            "resolved_sha": _BLESSED.sha,
        }
    )
    refreshed = _refresh_stable_variant_pin(trial)
    assert refreshed["resolved_sha"] == "i" * 40
    assert refreshed["source"] == "https://x/installed"
    assert trial.harbor_sha == "i" * 40


def test_the_installed_descriptor_reads_pep610_metadata():
    """Against this repo's own venv the metadata resolves to the locked
    default pin -- the ground truth the fallback also names."""
    from oddish.workers.queue.trial_handler import _installed_harbor_descriptor

    source, sha = _installed_harbor_descriptor()
    assert source == HARBOR_DEFAULT_SOURCE
    assert sha == HARBOR_DEFAULT_SHA


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
    result = _refresh_stable_variant_pin(
        trial, executing=(_BLESSED.source, _BLESSED.sha)
    )
    assert result is config
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
    config = {"analysis_payload": {"k": 1}}
    trial = _trial(config)
    assert _refresh_stable_variant_pin(trial) is config
    assert "resolved_sha" not in trial.harbor_config
    assert trial.harbor_sha is None


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
