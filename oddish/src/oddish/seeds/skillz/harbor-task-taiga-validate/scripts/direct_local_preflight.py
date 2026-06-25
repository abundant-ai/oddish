#!/usr/bin/env python3
"""Self-contained local auth preflight for direct Taiga validation.

Mirrors the auth handling in harbor-to-taiga/scripts/taiga_api.py:
  - Prefer TAIGA_TOKEN env var.
  - Else read ~/.config/taiga/auth.json and use the stored id_token.
  - When the id_token is expired (or --force-login is set) and a refresh_token
    is present, refresh it via Google's OAuth token endpoint and write the
    refreshed id_token back to auth.json. No external CLI required.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TAIGA_URL = "https://taiga.ant.dev"
REGISTRY_HOST = "us-east1-docker.pkg.dev"
DEFAULT_AUTH_PATH = Path.home() / ".config" / "taiga" / "auth.json"

GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_CLIENT_ID = os.environ.get(
    "TAIGA_OAUTH_CLIENT_ID",
    "REDACTED_CLIENT_ID",
)
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get(
    "TAIGA_OAUTH_CLIENT_SECRET",
    "REDACTED_CLIENT_SECRET",
)


def _run(cmd: list[str], *, capture: bool = False) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=capture)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() if capture else ""
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}"
            + (f"\nstderr:\n{stderr}" if stderr else "")
        )
    return proc.stdout if capture else ""


def _refresh_id_token(*, refresh_token: str) -> dict[str, Any]:
    data = urllib.parse.urlencode(
        {
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": (
                "openid https://www.googleapis.com/auth/userinfo.email "
                "https://www.googleapis.com/auth/userinfo.profile"
            ),
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url=GOOGLE_OAUTH_TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=data,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Failed to refresh Taiga token: HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to refresh Taiga token: {exc}") from exc

    if not payload.get("id_token"):
        raise RuntimeError("Failed to refresh Taiga token: response missing id_token")
    return payload


def _load_token(*, force_refresh: bool = False) -> str:
    env_token = os.environ.get("TAIGA_TOKEN", "").strip()
    if env_token:
        return env_token

    if not DEFAULT_AUTH_PATH.exists():
        raise RuntimeError(
            f"No TAIGA_TOKEN set and {DEFAULT_AUTH_PATH} not found. "
            "Run an initial Taiga login to populate the auth file."
        )

    try:
        payload = json.loads(DEFAULT_AUTH_PATH.read_text())
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse auth token from {DEFAULT_AUTH_PATH}: {exc}"
        ) from exc

    expires_at = payload.get("expires_at")
    refresh_token = payload.get("refresh_token")
    now = time.time()
    needs_refresh = force_refresh or (
        not isinstance(expires_at, (int, float)) or float(expires_at) <= now + 90
    )
    if needs_refresh:
        if not refresh_token:
            raise RuntimeError(
                f"Taiga id_token in {DEFAULT_AUTH_PATH} is expired and no refresh_token "
                "is present. Re-run an initial Taiga login to repopulate the auth file."
            )
        refreshed = _refresh_id_token(refresh_token=refresh_token)
        payload["id_token"] = refreshed["id_token"]
        payload["expires_at"] = now + int(refreshed.get("expires_in", 3600)) - 60
        DEFAULT_AUTH_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    token = payload.get("id_token")
    if not token:
        raise RuntimeError(f"Token missing in {DEFAULT_AUTH_PATH}")
    return token


def _verify_taiga_token(token: str) -> dict:
    req = urllib.request.Request(
        f"{TAIGA_URL}/api/my-user",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Unexpected Taiga status: {resp.status}")
        return json.loads(resp.read().decode())


def verify_taiga(*, force_refresh: bool = False) -> None:
    token = _load_token(force_refresh=force_refresh)
    try:
        user = _verify_taiga_token(token)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        if exc.code == 401 and not force_refresh:
            # Token in file may be valid by clock but rejected; force a refresh once.
            token = _load_token(force_refresh=True)
            user = _verify_taiga_token(token)
        else:
            raise RuntimeError(
                f"Taiga auth verification failed ({exc.code}): {body}"
            ) from exc
    print("Taiga auth OK")
    print(f"  Email: {user.get('email', '<unknown-email>')}")
    print(f"  Organization: {user.get('organization_name', '<unknown-org>')}")


def verify_registry() -> None:
    _run(["gcloud", "auth", "print-access-token"], capture=True)
    _run(["gcloud", "auth", "configure-docker", REGISTRY_HOST, "-q"])
    print("Artifact Registry auth OK")
    print(f"  Host: {REGISTRY_HOST}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Force a refresh of the Taiga id_token using the stored refresh_token.",
    )
    parser.add_argument(
        "--skip-registry",
        action="store_true",
        help="Skip Artifact Registry auth preflight.",
    )
    args = parser.parse_args()

    verify_taiga(force_refresh=args.force_login)

    if not args.skip_registry:
        verify_registry()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
