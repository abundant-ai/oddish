"""
Deployment entrypoint that registers all Modal functions.

Use: modal deploy backend/deploy.py
"""

import modal

from modal_app import (
    ENABLE_BACKGROUND_WORKERS,
    ENABLE_SLACK_EXPENSE_NOTIFICATIONS,
    app,
    assert_gke_cluster_exists,
)

# Import modules for side-effect registration of Modal functions.
import endpoints  # noqa: F401

if ENABLE_BACKGROUND_WORKERS:
    import worker  # noqa: F401

if ENABLE_SLACK_EXPENSE_NOTIFICATIONS:
    import slack_notifications  # noqa: F401

# Always register the answer worker. Mention routing remains gated by the
# deploy-baked flag, but a runtime secret can safely turn routing on without
# pointing the webhook at a function that does not exist.
import carl_agent  # noqa: F401, E402

# Deploy-time only: containers import the registration modules above, never
# execute this entrypoint locally, so the gate cannot run in workers.
if modal.is_local():
    assert_gke_cluster_exists()

__all__ = ["app"]
