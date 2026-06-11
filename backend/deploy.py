"""
Deployment entrypoint that registers all Modal functions.

Use: modal deploy backend/deploy.py
"""

from modal_app import ENABLE_BACKGROUND_WORKERS, app

# Import modules for side-effect registration of Modal functions.
import endpoints  # noqa: F401
import mcp_s3_app  # noqa: F401  (registers the S3 MCP asgi app)

if ENABLE_BACKGROUND_WORKERS:
    import worker  # noqa: F401

__all__ = ["app"]
