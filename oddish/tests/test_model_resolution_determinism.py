"""Curated model resolution must not depend on process credentials.

``auto_resolve_curated_model`` used to pick a provider by asking
``has_provider_credential`` -- i.e. ``os.getenv`` -- which made the resolved
model id a property of *whichever process resolved it*. That resolver runs in
the CLI and again on the API, and hosted Modal containers do not all carry the
same provider secrets, so the same raw submission could become
``deepseek/deepseek-v4-flash`` on one attempt and
``fireworks/deepseek-v4-flash-0731`` on the next. Two consequences:

* a laptop's ``DEEPSEEK_API_KEY`` decided which provider Oddish Cloud billed;
* ``compute_request_hash`` of the mutated submission moved with it, so an
  honest retry could 409 with "already used with a different request".

Resolution is now a pure function of ``(agent, model, explicit_provider)`` plus
the curated alias tables. These tests hold that line across the whole catalog.
"""

from __future__ import annotations

import itertools
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish import config as config_mod  # noqa: E402
from oddish.config import (  # noqa: E402
    _DEEPSEEK_MODEL_ALIASES,
    _FIREWORKS_SHORT_MODEL_IDS,
    apply_model_catalog_overlay,
    auto_resolve_curated_model,
    settings,
)
from oddish.core import sweeps as sweeps_mod  # noqa: E402
from oddish.core.idempotency import (  # noqa: E402
    STATUS_COMPLETED,
    SWEEP_ROUTE,
    IdempotencyConflict,
    IdempotencyReplay,
    StoredIdempotencyRecord,
    compute_request_hash,
    hash_idempotency_key,
    reserve_idempotency_slot,
)
from oddish.core.sweeps import validate_sweep_submission  # noqa: E402
from oddish.db.models import utcnow  # noqa: E402
from oddish.schemas import AgentModelPair, TaskSweepSubmission  # noqa: E402

# The two curated providers whose keys the resolver used to sniff.
_CRED_ENV = ("FIREWORKS_API_KEY", "DEEPSEEK_API_KEY")
# Every visibility a process can have: neither key, one, the other, both.
_CRED_COMBOS = tuple(itertools.product((False, True), repeat=len(_CRED_ENV)))

_AGENTS = (
    "mini-swe-agent",
    "claude-code",
    "codex",
    "dsh",
    "grok-build",
    "gemini-cli",
    "  DSH  ",  # locked agent, unnormalized
    "",
    None,
)

# Spellings that are not in the curated tables but must still resolve stably.
_EXTRA_SPELLINGS = (
    None,
    "",
    "   ",
    "none",
    "-",
    "gpt-5.2",
    "claude-sonnet-4-5",
    "azure/my-deployment",
    "fireworks/this-is-not-a-real-model",
    "deepseek/this-is-not-a-real-model",
    "accounts/fireworks/models/glm-5p2",
    "  DeepSeek-V4-Flash  ",
    "DEEPSEEK-V4-FLASH",
    "deepseek-v4-flash-0731",  # fireworks-only route; no deepseek alias
)


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    """Pin the ambient environment so a developer's shell cannot skew a run."""
    monkeypatch.delenv("ODDISH_MODEL_CATALOG_OVERLAY", raising=False)
    monkeypatch.delenv("ODDISH_ENFORCE_MODEL_CONCURRENCY", raising=False)
    monkeypatch.delenv("ODDISH_ENFORCE_MODEL_CREDENTIALS", raising=False)
    for name in _CRED_ENV:
        monkeypatch.delenv(name, raising=False)


def _apply_credentials(monkeypatch, combo: tuple[bool, ...]) -> None:
    for name, present in zip(_CRED_ENV, combo):
        if present:
            monkeypatch.setenv(name, f"sk-test-{name.lower()}")
        else:
            monkeypatch.delenv(name, raising=False)


def _curated_spellings() -> list[str]:
    """Every alias, canonical id, and provider-prefixed form in the catalog.

    Derived from the live tables so a new curated model is stress-tested the
    moment it is added.
    """
    out: set[str] = set()
    for alias, canonical in _FIREWORKS_SHORT_MODEL_IDS.items():
        out.update({alias, canonical, f"fireworks/{alias}", f"fw/{alias}"})
    for alias, canonical in _DEEPSEEK_MODEL_ALIASES.items():
        out.update({alias, canonical, f"deepseek/{alias}", f"ds/{alias}"})
    return sorted(out)


def _all_spellings() -> list[object]:
    return [*_curated_spellings(), *_EXTRA_SPELLINGS]


def _outcome(agent, model, **kwargs):
    """Resolution result as a comparable value, raises included."""
    try:
        return ("ok", *auto_resolve_curated_model(agent, model, **kwargs))
    except ValueError as exc:
        return ("error", str(exc))


# ---------------------------------------------------------------------------
# 1. The resolver is a pure function
# ---------------------------------------------------------------------------


def test_resolution_identical_under_every_credential_combination(monkeypatch):
    """The whole catalog x every agent x every key visibility: one answer each."""
    divergent = []
    for agent in _AGENTS:
        for model in _all_spellings():
            by_combo = {}
            for combo in _CRED_COMBOS:
                _apply_credentials(monkeypatch, combo)
                by_combo[combo] = _outcome(agent, model)
            if len(set(by_combo.values())) != 1:
                divergent.append((agent, model, by_combo))
    assert not divergent, f"credential-dependent resolution: {divergent}"


def test_resolution_never_reads_provider_credentials(monkeypatch):
    """Structural guard: the resolver must not call the credential probe at all.

    Value-equality above can be satisfied by accident; this fails the moment
    anyone reintroduces a credential read into the resolution path.
    """

    def _forbidden(provider):  # pragma: no cover - only runs on regression
        raise AssertionError(
            f"auto_resolve_curated_model consulted credentials for {provider!r}"
        )

    monkeypatch.setattr(config_mod, "has_provider_credential", _forbidden)
    for agent in _AGENTS:
        for model in _all_spellings():
            _outcome(agent, model)


def test_submit_validation_never_reads_provider_credentials(monkeypatch):
    """The same guard one layer up, with the self-host enforcement flag off."""

    def _forbidden(provider):  # pragma: no cover - only runs on regression
        raise AssertionError(f"sweep validation consulted credentials for {provider!r}")

    monkeypatch.setattr(sweeps_mod, "has_provider_credential", _forbidden)
    for model in ("deepseek-v4-flash", "fireworks/glm-5.2", "gpt-5.2"):
        validate_sweep_submission(_sweep("mini-swe-agent", model))


@pytest.mark.parametrize("combo", _CRED_COMBOS)
def test_resolution_is_a_fixed_point(monkeypatch, combo):
    """Re-resolving a resolved id returns it unchanged (no oscillation)."""
    _apply_credentials(monkeypatch, combo)
    for agent in _AGENTS:
        for model in _all_spellings():
            first = _outcome(agent, model)
            if first[0] != "ok" or not first[1]:
                continue
            again = _outcome(agent, first[1])
            assert again[0] == "ok"
            assert again[1] == first[1], (agent, model)


# ---------------------------------------------------------------------------
# 2. The specific flips the old credential sniffing caused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo", _CRED_COMBOS)
def test_bare_deepseek_flash_always_pins_fireworks(monkeypatch, combo):
    """A local DeepSeek key must not move a bare flash id off Fireworks."""
    _apply_credentials(monkeypatch, combo)
    resolved, reason = auto_resolve_curated_model("mini-swe-agent", "deepseek-v4-flash")
    assert resolved == "fireworks/deepseek-v4-flash-0731"
    assert reason == (
        "auto-selected fireworks/deepseek-v4-flash-0731"
        " for bare id 'deepseek-v4-flash'"
    )


@pytest.mark.parametrize("combo", _CRED_COMBOS)
def test_dsh_always_pins_deepseek(monkeypatch, combo):
    """A Fireworks key must not pull the DeepSeek-locked harness off DeepSeek."""
    _apply_credentials(monkeypatch, combo)
    resolved, _ = auto_resolve_curated_model("dsh", "deepseek-v4-flash")
    assert resolved == "deepseek/deepseek-v4-flash"


@pytest.mark.parametrize("combo", _CRED_COMBOS)
def test_reason_never_cites_credentials(monkeypatch, combo):
    """The old reason string leaked which key the resolving process held."""
    _apply_credentials(monkeypatch, combo)
    for agent in _AGENTS:
        for model in _all_spellings():
            outcome = _outcome(agent, model)
            if outcome[0] != "ok":
                continue
            reason = outcome[2]
            assert reason is None or "credential" not in reason.lower()


def test_locked_agent_with_no_matching_route_still_raises(monkeypatch):
    """`deepseek-v4-flash-0731` has only a Fireworks route; `dsh` cannot use it."""
    for combo in _CRED_COMBOS:
        _apply_credentials(monkeypatch, combo)
        with pytest.raises(ValueError, match="locked to provider 'deepseek'"):
            auto_resolve_curated_model("dsh", "deepseek-v4-flash-0731")


@pytest.mark.parametrize("combo", _CRED_COMBOS)
def test_explicit_provider_and_prefix_short_circuit(monkeypatch, combo):
    """Explicit intent wins and is never re-decided from the environment."""
    _apply_credentials(monkeypatch, combo)
    assert auto_resolve_curated_model(
        "mini-swe-agent", "deepseek-v4-flash", explicit_provider="deepseek"
    ) == ("deepseek-v4-flash", None)
    assert auto_resolve_curated_model(
        "mini-swe-agent", "deepseek/deepseek-v4-flash"
    ) == ("deepseek/deepseek-v4-flash", None)


def test_overlay_aliases_resolve_deterministically(monkeypatch):
    """A deploy-private alias auto-pins by the same rule, not by key presence."""
    before_fw = dict(config_mod._FIREWORKS_SHORT_MODEL_IDS)
    before_ds = dict(config_mod._DEEPSEEK_MODEL_ALIASES)
    monkeypatch.setenv(
        "ODDISH_MODEL_CATALOG_OVERLAY",
        '{"fireworks": {"deepseek-v9-secret": "deepseek-v9-secret-0101"},'
        ' "deepseek": {"deepseek-v9-secret": "deepseek-v9-secret"}}',
    )
    try:
        apply_model_catalog_overlay()
        seen = set()
        for combo in _CRED_COMBOS:
            _apply_credentials(monkeypatch, combo)
            seen.add(_outcome("mini-swe-agent", "deepseek-v9-secret"))
        expected_reason = (
            "auto-selected fireworks/deepseek-v9-secret-0101"
            " for bare id 'deepseek-v9-secret'"
        )
        assert seen == {("ok", "fireworks/deepseek-v9-secret-0101", expected_reason)}
    finally:
        config_mod._FIREWORKS_SHORT_MODEL_IDS.clear()
        config_mod._FIREWORKS_SHORT_MODEL_IDS.update(before_fw)
        config_mod._DEEPSEEK_MODEL_ALIASES.clear()
        config_mod._DEEPSEEK_MODEL_ALIASES.update(before_ds)


# ---------------------------------------------------------------------------
# 3. Submit-time validation inherits the determinism
# ---------------------------------------------------------------------------


def _sweep(agent: str, model, **kwargs) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="task-1",
        experiment_id="exp-1",
        configs=[AgentModelPair(agent=agent, model=model, n_trials=1, **kwargs)],
    )


_SUBMIT_CASES = (
    ("mini-swe-agent", "deepseek-v4-flash", {}),
    ("mini-swe-agent", "fireworks/deepseek-v4-flash", {}),
    ("mini-swe-agent", "glm-5.2", {}),
    ("mini-swe-agent", "fireworks/glm-5.2", {}),
    ("dsh", "deepseek-v4-flash", {}),
    ("codex", "azure/my-deployment", {}),
    ("claude-code", "claude-sonnet-4-5", {}),
    ("mini-swe-agent", "fireworks/nope", {"allow_unknown_model": True}),
)


@pytest.mark.parametrize(("agent", "model", "extra"), _SUBMIT_CASES)
def test_validated_model_identical_under_every_credential_combination(
    monkeypatch, agent, model, extra
):
    resolved = set()
    for combo in _CRED_COMBOS:
        _apply_credentials(monkeypatch, combo)
        submission = _sweep(agent, model, **extra)
        validate_sweep_submission(submission)
        resolved.add(submission.configs[0].model)
    assert len(resolved) == 1, f"{agent}/{model} resolved to {resolved}"


@pytest.mark.parametrize(("agent", "model", "extra"), _SUBMIT_CASES)
def test_post_validation_request_hash_is_stable(monkeypatch, agent, model, extra):
    """The fingerprint the server compares must not move with container secrets.

    This is the 409 Kyle reproduced: two attempts served by containers with
    different provider secrets hashed to different values under the same key.
    """
    hashes = set()
    for combo in _CRED_COMBOS:
        _apply_credentials(monkeypatch, combo)
        submission = _sweep(agent, model, **extra)
        validate_sweep_submission(submission)
        hashes.add(compute_request_hash(submission))
    assert len(hashes) == 1


def test_raw_hash_differs_from_post_validation_hash(monkeypatch):
    """Why the route hashes first: validation rewrites the body it would hash.

    Determinism already keeps the post-validation hash stable, but the raw hash
    is the client's actual bytes, so it stays correct even if some later
    validation step becomes environment-sensitive again.
    """
    submission = _sweep("mini-swe-agent", "deepseek-v4-flash")
    raw = compute_request_hash(submission)
    validate_sweep_submission(submission)
    assert submission.configs[0].model == "fireworks/deepseek-v4-flash-0731"
    assert compute_request_hash(submission) != raw


def test_unknown_curated_id_still_rejected_under_every_combination(monkeypatch):
    """Fail-closed admission must not become key-dependent either."""
    for combo in _CRED_COMBOS:
        _apply_credentials(monkeypatch, combo)
        with pytest.raises(HTTPException) as exc:
            validate_sweep_submission(_sweep("mini-swe-agent", "fireworks/nope"))
        assert exc.value.status_code == 422


def test_dsh_fireworks_only_id_rejected_at_submit(monkeypatch):
    """The locked-agent error the CLI no longer raises locally still 422s here."""
    for combo in _CRED_COMBOS:
        _apply_credentials(monkeypatch, combo)
        with pytest.raises(HTTPException) as exc:
            validate_sweep_submission(_sweep("dsh", "deepseek-v4-flash-0731"))
        assert exc.value.status_code == 422
        assert "locked to provider 'deepseek'" in str(exc.value.detail)


def test_enforce_credentials_rejects_but_never_rewrites(monkeypatch):
    """The self-host opt-in may refuse a submission; it must not change the id."""
    monkeypatch.setenv("ODDISH_ENFORCE_MODEL_CREDENTIALS", "1")
    monkeypatch.setenv("FIREWORKS_API_KEY", "sk-present")
    submission = _sweep("mini-swe-agent", "deepseek-v4-flash")
    validate_sweep_submission(submission)
    assert submission.configs[0].model == "fireworks/deepseek-v4-flash-0731"

    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        validate_sweep_submission(_sweep("mini-swe-agent", "deepseek-v4-flash"))
    assert exc.value.status_code == 422
    assert "No credential in this process" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# 4. End-to-end idempotency: Kyle's repro, without a database
# ---------------------------------------------------------------------------


class _FakeIdempotencyStore:
    """In-memory stand-in for ``SubmissionIdempotencyStore``."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], StoredIdempotencyRecord] = {}

    async def get(self, org_id, route, key_hash):
        return self.rows.get((org_id, route, key_hash))

    async def begin(self, org_id, route, key_hash, request_hash, now, expires_at):
        slot = (org_id, route, key_hash)
        if slot in self.rows:
            return False
        self.rows[slot] = StoredIdempotencyRecord(
            request_hash=request_hash,
            status="in_progress",
            response_json=None,
            expires_at=expires_at,
        )
        return True

    async def complete(self, org_id, route, key_hash, response_json):
        current = self.rows[(org_id, route, key_hash)]
        self.rows[(org_id, route, key_hash)] = StoredIdempotencyRecord(
            request_hash=current.request_hash,
            status=STATUS_COMPLETED,
            response_json=response_json,
            expires_at=current.expires_at,
        )

    async def discard(self, org_id, route, key_hash, now):
        self.rows.pop((org_id, route, key_hash), None)


async def _reserve(store, request_hash, key="client-key"):
    return await reserve_idempotency_slot(
        store,
        org_id="org-1",
        route=SWEEP_ROUTE,
        raw_key=key,
        request_hash=request_hash,
        now=utcnow(),
    )


@pytest.mark.asyncio
async def test_retry_replays_across_differing_container_credentials(monkeypatch):
    """Two attempts, two credential environments, one key: the retry replays.

    Reproduces the reported failure end to end. Each attempt hashes the raw
    client body exactly as the route now does, validates it (the mutation that
    used to be credential-dependent), and reserves the slot.
    """
    store = _FakeIdempotencyStore()
    raw_body = _sweep("mini-swe-agent", "deepseek-v4-flash")

    # Attempt 1 lands on a container that can see the DeepSeek key.
    _apply_credentials(monkeypatch, (False, True))
    first = _sweep("mini-swe-agent", "deepseek-v4-flash")
    first_hash = compute_request_hash(first)
    validate_sweep_submission(first)
    await _reserve(store, first_hash)
    await store.complete(
        "org-1", SWEEP_ROUTE, hash_idempotency_key("client-key"), {"trials_count": 1}
    )

    # Attempt 2 is a faithful retry that lands on a container that cannot.
    _apply_credentials(monkeypatch, (True, False))
    second = _sweep("mini-swe-agent", "deepseek-v4-flash")
    second_hash = compute_request_hash(second)
    validate_sweep_submission(second)

    assert second_hash == first_hash == compute_request_hash(raw_body)
    assert second.configs[0].model == first.configs[0].model
    with pytest.raises(IdempotencyReplay) as replay:
        await _reserve(store, second_hash)
    assert replay.value.response_json == {"trials_count": 1}


@pytest.mark.asyncio
async def test_retry_replays_even_when_hashed_after_mutation(monkeypatch):
    """Defense in depth: the old hash-after-validate ordering is safe too.

    The route now hashes first, but determinism means even the previous
    ordering can no longer produce a spurious conflict.
    """
    store = _FakeIdempotencyStore()

    _apply_credentials(monkeypatch, (False, True))
    first = _sweep("mini-swe-agent", "deepseek-v4-flash")
    validate_sweep_submission(first)
    await _reserve(store, compute_request_hash(first))
    await store.complete(
        "org-1", SWEEP_ROUTE, hash_idempotency_key("client-key"), {"trials_count": 1}
    )

    _apply_credentials(monkeypatch, (True, False))
    second = _sweep("mini-swe-agent", "deepseek-v4-flash")
    validate_sweep_submission(second)
    with pytest.raises(IdempotencyReplay):
        await _reserve(store, compute_request_hash(second))


@pytest.mark.asyncio
async def test_genuinely_different_submission_still_conflicts(monkeypatch):
    """Determinism must not blunt the guard: a changed body still 409s."""
    store = _FakeIdempotencyStore()

    first = _sweep("mini-swe-agent", "deepseek-v4-flash")
    await _reserve(store, compute_request_hash(first))
    await store.complete(
        "org-1", SWEEP_ROUTE, hash_idempotency_key("client-key"), {"trials_count": 1}
    )

    different = TaskSweepSubmission(
        task_id="task-1",
        experiment_id="exp-1",
        configs=[
            AgentModelPair(agent="mini-swe-agent", model="deepseek-v4-flash", n_trials=4)
        ],
    )
    with pytest.raises(IdempotencyConflict):
        await _reserve(store, compute_request_hash(different))


@pytest.mark.asyncio
async def test_expired_record_is_pruned_not_replayed():
    """Unchanged TTL behaviour, asserted against the same fake store."""
    store = _FakeIdempotencyStore()
    submission = _sweep("mini-swe-agent", "deepseek-v4-flash")
    request_hash = compute_request_hash(submission)
    key_hash = hash_idempotency_key("client-key")

    store.rows[("org-1", SWEEP_ROUTE, key_hash)] = StoredIdempotencyRecord(
        request_hash=request_hash,
        status=STATUS_COMPLETED,
        response_json={"stale": True},
        expires_at=utcnow() - timedelta(seconds=1),
    )
    reservation = await _reserve(store, request_hash)
    assert reservation.key_hash == key_hash


# ---------------------------------------------------------------------------
# 5. Storage/queue agreement for the resolved id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo", _CRED_COMBOS)
def test_queue_key_matches_stored_model_under_every_combination(monkeypatch, combo):
    """One model, one queue bucket -- independent of the resolving process."""
    _apply_credentials(monkeypatch, combo)
    submission = _sweep("mini-swe-agent", "deepseek-v4-flash")
    validate_sweep_submission(submission)
    model = submission.configs[0].model
    assert model == "fireworks/deepseek-v4-flash-0731"
    assert settings.normalize_trial_model("mini-swe-agent", model) == model
    assert settings.get_queue_key_for_trial("mini-swe-agent", model) == model
