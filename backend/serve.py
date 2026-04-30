"""Railway / vanilla uvicorn entrypoint."""

from __future__ import annotations

import logging

from oddish.config import Settings

Settings.db_use_null_pool = False
Settings.db_pool_size = 2
Settings.db_pool_max_overflow = 0

# Surface cc_chat orchestrator + daytona client logs. uvicorn configures
# handlers on its own loggers but leaves the root logger handlerless, so
# `cc_chat.*` log calls go nowhere by default. Attach a stream handler at
# INFO level so `send:` / `stream_logs:` / `sweeper:` lines hit the same
# stdout that uvicorn writes to.
_cc_chat_logger = logging.getLogger("cc_chat")
_cc_chat_logger.setLevel(logging.INFO)
if not _cc_chat_logger.handlers:
    _cc_chat_handler = logging.StreamHandler()
    _cc_chat_handler.setFormatter(
        logging.Formatter("%(levelname)s:     %(name)s: %(message)s")
    )
    _cc_chat_logger.addHandler(_cc_chat_handler)
    _cc_chat_logger.propagate = False

from api.app import create_app  # noqa: E402

api = create_app()

if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run("serve:api", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
