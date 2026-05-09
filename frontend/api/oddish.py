"""Vercel Python entrypoint for the Oddish Cloud API.

Vercel's Python runtime auto-detects the module-level ``app`` symbol as
an ASGI handler, so this file just wires up the existing FastAPI app
factory from ``backend/api/app.py`` and exposes it.

Routing model (see ``frontend/vercel.json``):

  Browser  ->  /py-api/<path>
              (rewritten by Vercel to)
              /api/oddish
              (handled by this function file; FastAPI's ``root_path``
               keeps OpenAPI links + redirect Location headers under
               the public ``/py-api`` prefix.)

Pool sizing differs from the Modal API container:

  * Modal containers are warm for minutes -> reuse pooled connections.
  * Vercel functions scale wide and are short-lived -> use NullPool so
    each invocation opens + closes its own connection. This avoids
    accumulating idle connections on Supavisor when Vercel scales out.

The ``backend/`` and ``oddish/src/`` source trees are bundled into the
function image via the ``includeFiles`` glob in ``vercel.json``. They
land at ``../backend`` and ``../oddish/src`` relative to this file,
mirroring their on-disk layout, and we add them to ``sys.path`` here so
the existing ``api.app`` / ``oddish.config`` imports keep working
unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FRONTEND_ROOT = _HERE.parent
_REPO_ROOT = _FRONTEND_ROOT.parent
sys.path.insert(0, str(_REPO_ROOT / "backend"))
sys.path.insert(0, str(_REPO_ROOT / "oddish" / "src"))

from oddish.config import Settings  # noqa: E402

Settings.db_use_null_pool = True
Settings.db_pool_size = 1
Settings.db_pool_max_overflow = 0

from api.app import create_app  # noqa: E402

app = create_app()
app.root_path = "/py-api"
