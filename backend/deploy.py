"""
Deployment entrypoint that registers all Modal functions.

Use: modal deploy backend/deploy.py
"""

from modal_app import app

# Import modules for side-effect registration of Modal functions.
import endpoints  # noqa: F401
import worker  # noqa: F401

__all__ = ["app"]
