"""Railway / vanilla uvicorn entrypoint."""
from __future__ import annotations

from oddish.config import Settings

Settings.db_use_null_pool = True

from api.app import create_app  # noqa: E402

api = create_app()

if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run("serve:api", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
