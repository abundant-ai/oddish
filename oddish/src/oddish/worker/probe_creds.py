from __future__ import annotations

import os

from oddish.config import api_base_url_for_modal_app
from oddish.core.api_keys import mint_internal_read_key
from oddish.db import get_session

PROBE_KEY_TTL_MINUTES = 240  # cover queue wait + run; trial timeouts are well under this


class ProbeCredsError(RuntimeError):
    pass


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
    base_url = resolve_probe_api_base_url()
    try:
        async with get_session() as session:
            key_id, raw_key = await mint_internal_read_key(
                session,
                org_id=org_id,
                name=f"probe:{trial_id}",
                ttl_minutes=PROBE_KEY_TTL_MINUTES,
            )
    except ProbeCredsError:
        raise
    except Exception as e:
        raise ProbeCredsError(f"minting probe read key failed: {e}") from e
    return key_id, {"ODDISH_API_KEY": raw_key, "ODDISH_API_BASE_URL": base_url}
