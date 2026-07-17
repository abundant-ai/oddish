"""One-off: report the applied Alembic revision of the *production* DB.

Two migration stacks share the prod database but stamp separate version
tables (see each stack's ``alembic/env.py``):

  * ``oddish/``  core stack  -> ``alembic_version_oddish``
  * ``backend/`` cloud stack -> ``alembic_version_backend``

``alembic current`` only reports the applied revision, which is exactly the
row in these tables. Querying them directly needs nothing but a DB session, so
it works in the deployed image without shipping the migration scripts. To see
whether prod is up to date, compare each value against ``uv run alembic heads``
run locally in the matching stack dir.

Runs in the deployed worker image with the ``oddish-prod`` secret. Read-only.

    cd backend
    modal run check_alembic_current_modal.py
"""

import modal

from modal_app import image, runtime_secrets

app = modal.App("check-alembic-current-oneoff")

_VERSION_TABLES = {
    "oddish (core)": "alembic_version_oddish",
    "backend (cloud)": "alembic_version_backend",
}


@app.function(image=image, secrets=runtime_secrets, timeout=120)
async def current() -> dict:
    from sqlalchemy import text

    from oddish.db import get_session

    out: dict[str, list[str]] = {}
    async with get_session() as session:
        for label, table in _VERSION_TABLES.items():
            try:
                result = await session.execute(
                    text(f"SELECT version_num FROM {table}")  # noqa: S608 (fixed identifiers)
                )
                out[label] = [row[0] for row in result.fetchall()]
            except Exception as exc:  # table missing => stack never migrated here
                out[label] = [f"<error: {type(exc).__name__}: {exc}>"]
    return out


@app.local_entrypoint()
def main() -> None:
    for label, revisions in current.remote().items():
        shown = ", ".join(revisions) if revisions else "<none applied>"
        print(f"{label:18} {shown}")
