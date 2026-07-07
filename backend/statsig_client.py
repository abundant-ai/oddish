"""Statsig gate check for BYOK.

A single feature gate, ``oddish_byok``, decides whether a user's own key is
used. It is managed entirely from the Statsig console. Everything degrades
safely: without ``STATSIG_SERVER_KEY`` the SDK never initializes and the gate
reads false, so BYOK is simply off.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

BYOK_GATE = "oddish_byok"

_statsig: Any = None
_init_lock = threading.Lock()
_init_failed = False


def reset_for_tests() -> None:
    global _statsig, _init_failed
    _statsig = None
    _init_failed = False


def _get_statsig() -> Any:
    """Lazy-init the server SDK once per process; None when unconfigured or broken.

    Modal containers don't fork after start, so post-import lazy init is safe
    (the SDK's fork warning is about pre-fork init in WSGI masters).
    """
    global _statsig, _init_failed
    if _statsig is not None or _init_failed:
        return _statsig
    server_key = os.environ.get("STATSIG_SERVER_KEY", "").strip()
    if not server_key:
        return None
    with _init_lock:
        if _statsig is not None or _init_failed:
            return _statsig
        try:
            from statsig_python_core import Statsig, StatsigOptions

            options = StatsigOptions()
            options.environment = os.environ.get("STATSIG_ENVIRONMENT", "production")
            client = Statsig(server_key, options)
            client.initialize().wait()
            _statsig = client
        except Exception:
            logger.warning("statsig init failed; BYOK gate treated as off", exc_info=True)
            _init_failed = True
    return _statsig


def _statsig_user(user_id: str, **custom: str | None) -> Any:
    from statsig_python_core import StatsigUser

    return StatsigUser(user_id=user_id, custom={k: v for k, v in custom.items() if v})


def byok_gate_passes(
    user_id: str,
    *,
    org_id: str | None = None,
    experiment_name: str | None = None,
    model: str | None = None,
    agent: str | None = None,
) -> bool:
    """Whether the BYOK gate is on for this user. False on any failure.

    The user id plus experiment/org/model/agent ride in as gate context, so
    console rules can target BYOK by any of them (e.g. a rule per experiment).
    """
    client = _get_statsig()
    if client is None:
        return False
    try:
        return bool(
            client.check_gate(
                _statsig_user(
                    user_id,
                    org_id=org_id,
                    experiment_name=experiment_name,
                    model=model,
                    agent=agent,
                ),
                BYOK_GATE,
            )
        )
    except Exception:
        logger.warning("statsig check_gate failed; treating as off", exc_info=True)
        return False
