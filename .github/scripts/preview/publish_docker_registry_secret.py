#!/usr/bin/env python3
"""Publish an optional per-preview Docker registry auth Modal secret."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys


def main() -> int:
    modal_environment = os.environ["MODAL_ENVIRONMENT"]
    modal_app_name = os.environ["MODAL_APP_NAME"]
    username = os.environ.get("ODDISH_GHCR_USERNAME") or os.environ.get("GITHUB_ACTOR")
    token = os.environ.get("ODDISH_GHCR_TOKEN")

    if not username or not token:
        print("Docker registry auth secret not configured; skipping")
        return 0

    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    payload = json.dumps(
        {"auths": {"ghcr.io": {"auth": auth}}},
        separators=(",", ":"),
    )
    secret_name = f"{modal_app_name}-registry-auth"
    subprocess.run(
        [
            "uv",
            "run",
            "modal",
            "secret",
            "create",
            "--env",
            modal_environment,
            "--force",
            secret_name,
            f"ODDISH_DOCKER_AUTH_CONFIG={payload}",
        ],
        check=True,
    )

    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"ODDISH_DOCKER_AUTH_SECRET_NAME={secret_name}\n")

    print(f"Published Docker registry auth Modal secret: {secret_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
