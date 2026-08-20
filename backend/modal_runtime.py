import os

import modal

MODAL_APP_NAME = os.environ.get("MODAL_APP_NAME", "oddish")
MODAL_SECRET_ENVIRONMENT = os.environ.get("MODAL_SECRET_ENVIRONMENT", "main")
RUNTIME_SECRET_NAME = "oddish-prod"

app = modal.App(MODAL_APP_NAME)
runtime_secret = modal.Secret.from_name(
    RUNTIME_SECRET_NAME, environment_name=MODAL_SECRET_ENVIRONMENT
)
