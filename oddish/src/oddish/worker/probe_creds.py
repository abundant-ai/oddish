from __future__ import annotations

import os
from typing import Awaitable, Callable

from oddish.config import api_base_url_for_modal_app

PROBE_KEY_TTL_MINUTES = 240  # cover queue wait + run; trial timeouts are well under this

# Minting/revoking an internal read key writes to ``api_keys`` — a hosted-only
# (cloud) table that core never imports. The cloud layer injects the real
# implementations at worker startup (see ``backend/worker/functions.py``). On a
# self-hosted/OSS worker no provider is registered, so probe creds simply can't
# be minted, which is correct: probes are a hosted feature.
MintReadKey = Callable[..., Awaitable[tuple[str, str]]]
DeleteKey = Callable[[str], Awaitable[None]]

_mint_read_key: MintReadKey | None = None
_delete_key: DeleteKey | None = None


class ProbeCredsError(RuntimeError):
    pass


def register_probe_creds_provider(
    *, mint_read_key: MintReadKey, delete_key: DeleteKey
) -> None:
    """Install the cloud implementations used to mint/revoke probe read keys."""
    global _mint_read_key, _delete_key
    _mint_read_key = mint_read_key
    _delete_key = delete_key


def _modal_fallback() -> str:
    try:
        return api_base_url_for_modal_app() or ""
    except Exception:
        return ""


def resolve_probe_api_base_url() -> str:
    url = os.environ.get("ODDISH_PUBLIC_API_BASE_URL") or _modal_fallback()
    if not url:
        raise ProbeCredsError("cannot resolve oddish API base URL for probe CLI")
    return url


async def mint_probe_creds(
    *, org_id: str | None, trial_id: str
) -> tuple[str, dict[str, str]]:
    """Returns (api_key_id, env) for the probe agent. Raises ProbeCredsError on failure."""
    if not org_id:
        raise ProbeCredsError("probe trial has no org_id; cannot mint read key")
    if _mint_read_key is None:
        raise ProbeCredsError(
            "no probe creds provider registered; minting read keys is a "
            "hosted-only capability"
        )
    base_url = resolve_probe_api_base_url()
    try:
        key_id, raw_key = await _mint_read_key(
            org_id=org_id,
            name=f"probe:{trial_id}",
            ttl_minutes=PROBE_KEY_TTL_MINUTES,
        )
    except ProbeCredsError:
        raise
    except Exception as e:
        raise ProbeCredsError(f"minting probe read key failed: {e}") from e
    return key_id, {"ODDISH_API_KEY": raw_key, "ODDISH_API_BASE_URL": base_url}


async def delete_probe_key(api_key_id: str) -> None:
    """Best-effort revoke a minted probe read key. No-op when no provider is set."""
    if _delete_key is None:
        return
    await _delete_key(api_key_id)
