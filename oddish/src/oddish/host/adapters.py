"""Host adapters: Modal, uvicorn, Docker (design spec §5.3 / §6.3).

``import modal`` stays lazy so importing this module never pulls the SDK into a
self-hosted process.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable

from oddish.host.ports import HostAdapter, SecretSource


class EnvSecretSource:
    """Reads runtime secrets from the process environment.

    Works for every host: Modal injects its Secrets as env vars at container
    start, Docker/uvicorn read the env directly.
    """

    def get(self, name: str) -> str | None:
        return os.environ.get(name)


async def run_periodic(
    fn: Callable[[], Awaitable[None]],
    *,
    period_s: float,
    _stop: Callable[[], bool] = lambda: False,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Self-host analog of ``modal.Period``: run ``fn`` every ``period_s``."""
    while not _stop():
        await fn()
        await _sleep(period_s)


class UvicornHostAdapter:
    """Serves ``create_app()`` under plain uvicorn (``backend/serve.py``)."""

    name = "uvicorn"

    def serve_api(
        self, app: object, *, host: str = "0.0.0.0", port: int = 8000
    ) -> None:
        import uvicorn

        uvicorn.run(app, host=host, port=port)

    async def schedule(
        self,
        fn: Callable[[], Awaitable[None]],
        *,
        period_s: float,
        _stop: Callable[[], bool] = lambda: False,
        _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        await run_periodic(fn, period_s=period_s, _stop=_stop, _sleep=_sleep)

    def secret_source(self) -> SecretSource:
        return EnvSecretSource()


class DockerHostAdapter(UvicornHostAdapter):
    """Docker host: identical serving to uvicorn (``backend/Dockerfile`` runs
    ``uvicorn serve:api``); named distinctly so config can select it."""

    name = "docker"


class ModalHostAdapter:
    """Modal host. Serving and scheduling are **declarative** (decorators in
    ``backend/endpoints.py`` / ``backend/worker/functions.py``), so the runtime
    methods point the caller at the manifest. This adapter also owns the
    dual-registration manifest concern (spec §4.1)."""

    name = "modal"

    def serve_api(
        self, app: object, *, host: str = "0.0.0.0", port: int = 8000
    ) -> None:
        raise NotImplementedError(
            "Modal hosting is declarative: create_app() is published via "
            "@modal.asgi_app in backend/endpoints.py, not served imperatively."
        )

    async def schedule(
        self, fn: Callable[[], Awaitable[None]], *, period_s: float
    ) -> None:
        raise NotImplementedError(
            "Modal scheduling is declarative: use schedule=modal.Period(...) on "
            "the function in backend/worker/functions.py."
        )

    def secret_source(self) -> SecretSource:
        # Modal Secrets are injected as env vars at container start.
        return EnvSecretSource()


_: HostAdapter = UvicornHostAdapter()  # structural conformance check at import
