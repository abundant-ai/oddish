from __future__ import annotations

import os
from pathlib import Path
import typer
from rich.console import Console

console = Console()
# Error console writes to stderr - important for --json mode where stdout is redirected
error_console = Console(stderr=True)

# =============================================================================
# Constants
# =============================================================================

DEFAULT_API_URL = os.environ.get(
    "ODDISH_DEFAULT_API_URL", "https://abundant-ai--api.modal.run"
)
DEFAULT_DASHBOARD_URL = os.environ.get(
    "ODDISH_DEFAULT_DASHBOARD_URL", "https://www.oddish.app"
)
LOCAL_API_URL = "http://localhost:8000"
LOCAL_DASHBOARD_URL = "http://localhost:3000"
API_LOG_PATH = Path("/tmp/oddish-api.log")
API_PID_PATH = Path("/tmp/oddish-api.pid")


# =============================================================================
# API URL Helpers
# =============================================================================


def get_api_url() -> str:
    """Get API URL from environment or default."""
    env_url = os.environ.get("ODDISH_API_URL")
    if env_url:
        return env_url
    return DEFAULT_API_URL


def is_local_api_url(api_url: str) -> bool:
    """Return True if the API URL targets localhost."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(api_url)
        host = (parsed.hostname or "").lower()
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "0.0.0.0"}


def is_modal_api_url(api_url: str) -> bool:
    """Return True if the API URL targets Modal Cloud."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(api_url)
        host = (parsed.hostname or "").lower()
    except Exception:
        return False
    return host.endswith(".modal.run")


def get_dashboard_url(api_url: str | None = None) -> str:
    """Get dashboard URL based on API URL or environment.

    Priority:
    1. ODDISH_DASHBOARD_URL environment variable
    2. Infer from API URL (local vs hosted)
    """
    env_url = os.environ.get("ODDISH_DASHBOARD_URL")
    if env_url:
        return env_url.rstrip("/")

    # Infer from API URL
    if api_url is None:
        api_url = get_api_url()

    if is_local_api_url(api_url):
        return LOCAL_DASHBOARD_URL
    return DEFAULT_DASHBOARD_URL


# =============================================================================
# Authentication
# =============================================================================


def get_api_key() -> str | None:
    """Get API key from environment."""
    env_key = os.environ.get("ODDISH_API_KEY")
    if env_key:
        return env_key
    return None


def require_api_key(api_url: str | None = None) -> str:
    """Require ODDISH_API_KEY for authenticated API access."""
    if api_url is None:
        api_url = get_api_url()
    api_key = get_api_key()
    if not api_key and is_local_api_url(api_url):
        return ""
    if not api_key:
        error_console.print(
            "[red]Missing API token.[/red]\n"
            "If you're running locally, set ODDISH_API_URL to http://localhost:8000 "
            "to skip auth.\n"
            f"Otherwise set ODDISH_API_KEY (create one at {DEFAULT_DASHBOARD_URL})."
        )
        raise typer.Exit(1)
    return api_key


def get_auth_headers(api_url: str | None = None) -> dict[str, str]:
    """Build auth headers for API requests."""
    api_key = require_api_key(api_url)
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}
