from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    API_LOG_PATH,
    API_PID_PATH,
    error_console,
    get_auth_headers,
)
from oddish.infra import (
    docker_available,
    ensure_postgres,
    postgres_container_exists,
    postgres_container_running,
)

console = Console()


# =============================================================================
# Process & Port Management
# =============================================================================


def get_db_url_from_env() -> str | None:
    """Read DATABASE_URL or ODDISH_DATABASE_URL from environment."""
    return os.environ.get("DATABASE_URL") or os.environ.get("ODDISH_DATABASE_URL")


def append_log(message: str) -> None:
    """Append a line to the Oddish API log."""
    try:
        with API_LOG_PATH.open("a") as log_file:
            log_file.write(f"{message}\n")
    except Exception:
        pass


def pid_is_running(pid: int) -> bool:
    """Check if a PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_api_pid() -> int | None:
    """Read the API PID from disk."""
    if not API_PID_PATH.exists():
        return None
    try:
        pid_text = API_PID_PATH.read_text().strip()
        return int(pid_text)
    except Exception:
        return None


def check_port_in_use(port: int) -> bool:
    """Check if a port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


# =============================================================================
# API Health & Lifecycle
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


def stop_api() -> bool:
    """Stop the API server if running. Returns True if stopped."""
    api_pid = read_api_pid()
    if api_pid and pid_is_running(api_pid):
        try:
            os.kill(api_pid, 15)  # SIGTERM
            API_PID_PATH.unlink(missing_ok=True)
            # Wait for it to actually stop
            for _ in range(10):
                if not pid_is_running(api_pid):
                    return True
                time.sleep(0.5)
            # Force kill if still running
            os.kill(api_pid, 9)  # SIGKILL
            return True
        except Exception:
            pass
    return False


# =============================================================================
# Database Setup
# =============================================================================


def run_db_setup(db_url: str, quiet: bool = False) -> bool:
    """Run database setup (migrations + PGQueuer). Returns True on success."""
    env = {**os.environ, "DATABASE_URL": db_url}

    # Run alembic migrations
    if not quiet:
        console.print("[dim]Running database migrations...[/dim]")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr or ""
        # Check if it's just "already at head" or a real error
        if "Target database is not up to date" not in stderr:
            first_line = stderr.splitlines()[0] if stderr else "unknown error"
            console.print(f"[yellow]Migration note:[/yellow] {first_line}")
            append_log(f"[db-migrations] {first_line}")
            if not quiet:
                console.print(f"[dim]See logs: {API_LOG_PATH}[/dim]")

    # Install PGQueuer tables using subprocess to ensure correct env
    # (Direct import would use stale settings from module load time)
    if not quiet:
        console.print("[dim]Installing queue tables...[/dim]")
    result = subprocess.run(
        [sys.executable, "-m", "oddish.db", "install-pgqueuer"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        # PGQueuer install is idempotent, ignore "already exists" errors
        stderr = result.stderr or ""
        if "already exists" not in stderr.lower() and "duplicate" not in stderr.lower():
            first_line = stderr.splitlines()[0] if stderr else "unknown error"
            console.print(f"[yellow]PGQueuer note:[/yellow] {first_line}")
            append_log(f"[pgqueuer-install] {first_line}")
            if not quiet:
                console.print(f"[dim]See logs: {API_LOG_PATH}[/dim]")

    return True


# =============================================================================
# Infrastructure Orchestration
# =============================================================================


def ensure_infrastructure(
    api_url: str,
    quiet: bool = False,
    concurrency: dict[str, int] | None = None,
    fresh: bool = False,
) -> None:
    """Ensure Postgres and API are running.

    Args:
        api_url: The API URL to check/use.
        quiet: If True, suppress informational logs (errors still shown).
        concurrency: Provider concurrency limits (e.g., {"claude": 8, "default": 4}).
        fresh: If True, restart the API even if it's already running.
    """
    # Handle --fresh flag: stop existing API first
    if fresh and check_api_health(api_url):
        if not quiet:
            console.print("[dim]Stopping existing API server (--fresh)...[/dim]")
        stop_api()
        time.sleep(1)  # Brief pause before restarting

    # Check if API is already running
    if check_api_health(api_url):
        if not quiet:
            console.print("[dim]Using existing Oddish API server[/dim]")
            console.print(
                "[dim]Tip: Use --fresh to restart with new settings, or 'oddish clean' to stop[/dim]"
            )
        return

    # If we have a PID file, the API may be starting already
    api_pid = read_api_pid()
    if api_pid and pid_is_running(api_pid):
        if not quiet:
            console.print("[dim]API process detected, waiting for readiness...[/dim]")
        for _ in range(15):
            if check_api_health(api_url):
                if not quiet:
                    console.print("[green]API server is ready[/green]")
                return
            time.sleep(1)
    elif api_pid:
        try:
            API_PID_PATH.unlink()
        except Exception:
            pass

    db_url = get_db_url_from_env()
    if db_url:
        os.environ["DATABASE_URL"] = db_url
        if not quiet:
            console.print("[dim]Using DATABASE_URL from environment[/dim]")
    else:
        # Check Docker
        if not docker_available():
            console.print(
                "[red]Docker is not available.[/red]\n"
                "Oddish requires Docker to manage the local Postgres database.\n"
                "Please install Docker and ensure it's running."
            )
            raise typer.Exit(1)

        if check_port_in_use(5432) and not postgres_container_running():
            console.print(
                "[yellow]Port 5432 is already in use.[/yellow]\n"
                "If another Postgres is running, set DATABASE_URL or stop it.\n"
                "If this is the oddish container, try: oddish run <task_dir>"
            )

    if check_port_in_use(8000) and not check_api_health(api_url):
        console.print(
            "[yellow]Port 8000 is already in use, but the API is not healthy.[/yellow]\n"
            "If another service is using the port, stop it or set --api to a different URL."
        )

    # Ensure Postgres is running (local Docker) if no external URL provided
    if not db_url:
        try:
            if not postgres_container_exists():
                if not quiet:
                    console.print("[dim]Starting local database (first run)...[/dim]")
            else:
                if not quiet:
                    console.print("[dim]Starting local database...[/dim]")
            db_url = ensure_postgres()
            os.environ["DATABASE_URL"] = db_url
        except RuntimeError as e:
            error_console.print(f"[red]Failed to start Postgres:[/red] {e}")
            raise typer.Exit(1)

    # Run database setup (migrations + PGQueuer)
    # This is idempotent - safe to run every time
    run_db_setup(db_url, quiet=quiet)

    # Start the API server in background
    if not quiet:
        console.print("[dim]Starting API server...[/dim]")

    # Start API as a background process
    # Use the same Python interpreter to run the API module
    # Log output to /tmp/oddish-api.log for debugging
    log_file = API_LOG_PATH.open("a")

    # Build command with optional concurrency settings
    api_cmd = [sys.executable, "-m", "oddish.api"]
    if concurrency:
        api_cmd.extend(["--n-concurrent", json.dumps(concurrency)])

    process = subprocess.Popen(
        api_cmd,
        stdout=log_file,
        stderr=log_file,
        env={**os.environ, "DATABASE_URL": db_url},
    )
    try:
        API_PID_PATH.write_text(str(process.pid))
    except Exception:
        pass

    # Wait for API to be ready
    for _ in range(30):  # 30 seconds max
        if check_api_health(api_url):
            if not quiet:
                console.print("[green]API server is ready[/green]")
            return
        time.sleep(1)

    error_console.print("[red]API server failed to start within 30 seconds[/red]")
    error_console.print(f"[dim]See logs: {API_LOG_PATH}[/dim]")
    error_console.print("[dim]Try: oddish logs[/dim]")
    raise typer.Exit(1)
