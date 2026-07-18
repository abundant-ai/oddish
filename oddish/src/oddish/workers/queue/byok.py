"""Per-trial BYOK (bring-your-own-key) env resolution seam.

Core defines the registry and pure helpers only. The hosted backend registers
an async resolver at worker startup (mirroring how ``functions.py`` calls
``ensure_builtin_handlers_registered()``); standalone oddish never registers
one, so ``resolve_byok`` returns ``None`` and behavior is unchanged.

The contract is deliberately fail-open: a trial only ever gains a user key, it
never loses the platform key. If resolution can't produce a usable key for any
reason, the trial runs on the platform key exactly as it would without BYOK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Mapping, Protocol

from rich.console import Console

console = Console()

_ANTHROPIC_PROVIDERS = frozenset({"anthropic", "claude"})


@dataclass(frozen=True)
class ByokResolution:
    """The env overlay to inject for one trial (empty means nothing to add)."""

    env: Mapping[str, str] = field(default_factory=dict)


class ByokResolver(Protocol):
    def __call__(
        self,
        *,
        owner_user_id: str | None,
        org_id: str | None,
        experiment_name: str | None,
        model: str | None,
        agent: str,
    ) -> Awaitable[ByokResolution | None]: ...


_RESOLVER: ByokResolver | None = None


def register_byok_resolver(fn: ByokResolver) -> None:
    global _RESOLVER
    _RESOLVER = fn


def clear_byok_resolver() -> None:
    global _RESOLVER
    _RESOLVER = None


def byok_resolver_registered() -> bool:
    return _RESOLVER is not None


async def resolve_byok(
    *,
    owner_user_id: str | None,
    org_id: str | None,
    experiment_name: str | None,
    model: str | None,
    agent: str,
) -> ByokResolution | None:
    """Call the registered resolver; any crash falls back to platform keys."""
    if _RESOLVER is None:
        return None
    try:
        return await _RESOLVER(
            owner_user_id=owner_user_id,
            org_id=org_id,
            experiment_name=experiment_name,
            model=model,
            agent=agent,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]BYOK resolver crashed (using platform keys): {exc}[/yellow]"
        )
        return None


def uses_direct_anthropic(agent: str, model: str | None, *, settings: Any) -> bool:
    """Whether the trial reaches the direct Anthropic API, so an injected
    ``ANTHROPIC_API_KEY`` actually takes effect.

    A plain Anthropic-style Claude id resolves to the "anthropic" provider and
    runs on the direct Anthropic API, where a user key applies. A
    Bedrock-shaped id resolves to "bedrock" and genuinely runs on AWS Bedrock,
    which authenticates with AWS credentials an API key can't serve -- so the
    resolver must not claim eligibility it can't deliver there.
    """
    provider = (settings.get_provider_for_trial(agent, model) or "").lower()
    return provider in _ANTHROPIC_PROVIDERS


def merge_byok_env(
    byok_env: Mapping[str, str] | None,
    probe_env: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Layer the BYOK env under probe creds (probe wins), preserving the
    ``extra_agent_env: dict | None`` contract (None stays None)."""
    if not byok_env and probe_env is None:
        return None
    merged = dict(byok_env or {})
    merged.update(probe_env or {})
    return merged or None
