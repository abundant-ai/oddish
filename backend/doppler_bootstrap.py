"""Load runtime secrets from Doppler before application settings initialize."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_LOADED = False


def _truthy(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _download_secrets(
    token: str, project: str | None, config: str | None
) -> dict[str, object]:
    params = {"format": "json"}
    if project:
        params["project"] = project
    if config:
        params["config"] = config
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"https://api.doppler.com/v3/configs/config/secrets/download?{query}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        raise RuntimeError(
            f"Doppler secrets download failed with HTTP {exc.code}: {payload}"
        ) from exc


def load_doppler_secrets() -> bool:
    """Populate ``os.environ`` from Doppler when DOPPLER_TOKEN is configured.

    This is deliberately opt-in: production keeps using existing platform
    secrets until a Doppler token secret is attached to the runtime.
    """
    global _LOADED
    if _LOADED:
        return True

    token = os.environ.get("DOPPLER_TOKEN")
    if not token:
        return False

    project = os.environ.get("DOPPLER_PROJECT")
    config = os.environ.get("DOPPLER_CONFIG")
    override = _truthy(os.environ.get("DOPPLER_SECRETS_OVERRIDE"), default=True)
    required = _truthy(os.environ.get("DOPPLER_SECRETS_REQUIRED"), default=True)

    try:
        secrets = _download_secrets(token, project, config)
    except Exception:
        if required:
            raise
        logger.exception("could not load Doppler secrets; continuing with platform env")
        return False

    loaded = 0
    for key, value in secrets.items():
        if not isinstance(value, str):
            continue
        if key in os.environ and not override:
            continue
        os.environ[key] = value
        loaded += 1

    _LOADED = True
    logger.info("loaded %s secret(s) from Doppler", loaded)
    return True
