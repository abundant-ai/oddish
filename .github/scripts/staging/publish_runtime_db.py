"""Publish staging's transaction-pool URL without changing its migration URL."""

import os
import subprocess
from urllib.parse import urlsplit, urlunsplit


def transaction_url(session_url: str) -> str:
    url = urlsplit(session_url)
    if (
        url.scheme != "postgresql+asyncpg"
        or not (url.hostname or "").endswith(".pooler.supabase.com")
        or url.port not in (5432, 6543)
        or not url.username
        or not url.password
        or url.path != "/postgres"
    ):
        raise ValueError("Expected a Supabase staging pooler URL on port 5432 or 6543")
    # Preserve the encoded credentials, host, database and query verbatim.
    netloc = url.netloc.rsplit(":", 1)[0] + ":6543"
    return urlunsplit(url._replace(netloc=netloc))


def main() -> None:
    runtime_url = transaction_url(os.environ["STAGING_DATABASE_URL"])
    print(f"::add-mask::{runtime_url}", flush=True)
    subprocess.run(
        [
            "uv",
            "run",
            "modal",
            "secret",
            "create",
            "--env",
            "staging",
            "--force",
            "oddish-staging-db",
            f"ODDISH_DATABASE_URL={runtime_url}",
            "ODDISH_DASHBOARD_URL=https://staging.oddish.app",
            "CORS_ALLOWED_ORIGINS=https://staging.oddish.app",
            "LOGFIRE_ENVIRONMENT=staging",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
