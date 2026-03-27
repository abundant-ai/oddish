from __future__ import annotations

import os

import httpx
import typer
from rich.console import Console

console = Console()
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


# =============================================================================
# API URL Helpers
# =============================================================================


def get_api_url() -> str:
    """Get API URL from environment or default."""
    env_url = os.environ.get("ODDISH_API_URL")
    if env_url:
        return env_url
    return DEFAULT_API_URL


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
    """Get dashboard URL from environment or default."""
    env_url = os.environ.get("ODDISH_DASHBOARD_URL")
    if env_url:
        return env_url.rstrip("/")
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
    api_key = get_api_key()
    if not api_key:
        error_console.print(
            "[red]Missing API token.[/red]\n"
            f"Set ODDISH_API_KEY (create one at {DEFAULT_DASHBOARD_URL})."
        )
        raise typer.Exit(1)
    return api_key


def get_auth_headers(api_url: str | None = None) -> dict[str, str]:
    """Build auth headers for API requests."""
    api_key = require_api_key(api_url)
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


# =============================================================================
# API Health
# =============================================================================


def check_api_health(api_url: str, timeout: float = 2.0) -> bool:
    """Check if the API is healthy."""
    try:
        with httpx.Client(timeout=timeout, headers=get_auth_headers()) as client:
            response = client.get(f"{api_url}/health")
            result: bool = response.status_code == 200
            return result
    except Exception:
        return False
