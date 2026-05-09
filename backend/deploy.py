"""
Deployment entrypoint that registers all Modal functions.

Use: modal deploy backend/deploy.py

Post-migration the HTTP API is served by Vercel Python functions
(see ``frontend/api/oddish.py`` + ``frontend/vercel.json``); Modal only
runs the worker dispatcher and single-job containers.

The legacy Modal-hosted FastAPI ASGI function in ``endpoints.py`` is
kept around for one release cycle as an emergency fallback. Set
``ODDISH_ENABLE_MODAL_API=1`` to re-register it during a rollback.
"""

import os

from modal_app import ENABLE_BACKGROUND_WORKERS, app

if os.environ.get("ODDISH_ENABLE_MODAL_API", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    import endpoints  # noqa: F401

if ENABLE_BACKGROUND_WORKERS:
    import worker  # noqa: F401

__all__ = ["app"]
