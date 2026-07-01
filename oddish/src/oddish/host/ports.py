"""The ``HostAdapter`` port (design spec §5.3).

Names the already-portable hosting seam. The core is ``api.app.create_app()``;
an adapter binds it to a runtime (Modal asgi / uvicorn / Docker), supplies a
secret source, and runs periodic jobs (the dispatcher + reconciler cadence). The
three adapters exist in embryo already — Modal (``backend/endpoints.py`` +
``backend/modal_app.py``), uvicorn (``backend/serve.py``), Docker
(``backend/Dockerfile``); this port just names them.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol, runtime_checkable


@runtime_checkable
class SecretSource(Protocol):
    """Runtime secret lookup. Modal injects its Secrets as env vars inside the
    container, so every host reads runtime secrets the same way — only the
    deploy-time mapping differs."""

    def get(self, name: str) -> str | None: ...


@runtime_checkable
class HostAdapter(Protocol):
    name: str

    def serve_api(self, app: object, *, host: str = "0.0.0.0", port: int = 8000) -> None:
        """Bind ``create_app()`` to this host's serving runtime."""
        ...

    async def schedule(
        self, fn: Callable[[], Awaitable[None]], *, period_s: float
    ) -> None:
        """Run ``fn`` on a fixed cadence (the dispatcher / reconciler timer)."""
        ...

    def secret_source(self) -> SecretSource:
        """Return this host's runtime secret lookup."""
        ...
