import subprocess
import time

from rich.console import Console

console = Console()

# Docker container configuration
CONTAINER_NAME = "oddish-db"
VOLUME_NAME = "oddish-data"
IMAGE = "postgres:16-alpine"
PORT = 5432
POSTGRES_USER = "oddish"
POSTGRES_PASSWORD = "oddish"
POSTGRES_DB = "oddish"


def _run_docker(
    args: list[str], capture: bool = True, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a docker command."""
    cmd = ["docker"] + args
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
    )


def _container_exists() -> bool:
    """Check if the oddish-db container exists."""
    result = _run_docker(["inspect", CONTAINER_NAME], check=False)
    return result.returncode == 0


def _container_running() -> bool:
    """Check if the oddish-db container is running."""
    result = _run_docker(
        ["inspect", "-f", "{{.State.Running}}", CONTAINER_NAME],
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def postgres_container_exists() -> bool:
    """Public check for whether the Postgres container exists."""
    return _container_exists()


def postgres_container_running() -> bool:
    """Public check for whether the Postgres container is running."""
    return _container_running()


def _wait_for_postgres(timeout: int = 30) -> bool:
    """Wait for Postgres to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        result = _run_docker(
            [
                "exec",
                CONTAINER_NAME,
                "pg_isready",
                "-U",
                POSTGRES_USER,
                "-d",
                POSTGRES_DB,
            ],
            check=False,
        )
        if result.returncode == 0:
            return True
        time.sleep(0.5)
    return False


def docker_available() -> bool:
    """Check if Docker is available and running."""
    try:
        result = _run_docker(["info"], check=False)
        return result.returncode == 0
    except FileNotFoundError:
        return False


def ensure_postgres() -> str:
    """Ensure Postgres container is running. Returns connection URL.

    This is the main entry point for infrastructure management.
    It will:
    1. Check if Docker is available
    2. Create the container if it doesn't exist
    3. Start the container if it's not running
    4. Wait for Postgres to be ready
    5. Return the connection URL

    Returns:
        Connection URL for the Postgres database.

    Raises:
        RuntimeError: If Docker is not available or Postgres fails to start.
    """
    if not docker_available():
        raise RuntimeError(
            "Docker is not available. Please install Docker and ensure it's running.\n"
            "Oddish requires Docker to manage the local Postgres database."
        )

    if not _container_exists():
        console.print(f"[dim]Creating {CONTAINER_NAME} container...[/dim]")

        # Try to pull the image first to get better error messages
        try:
            console.print(f"[dim]Pulling {IMAGE} image...[/dim]")
            result = _run_docker(["pull", IMAGE], check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to pull Docker image '{IMAGE}'.\n"
                    f"Error: {result.stderr}\n"
                    f"Please check your internet connection and Docker configuration."
                )
        except Exception as e:
            raise RuntimeError(
                f"Failed to pull Docker image '{IMAGE}': {e}\n"
                f"Please check your internet connection and Docker configuration."
            ) from e

        try:
            _run_docker(
                [
                    "run",
                    "-d",
                    "--name",
                    CONTAINER_NAME,
                    "-v",
                    f"{VOLUME_NAME}:/var/lib/postgresql/data",
                    "-p",
                    f"{PORT}:5432",
                    "-e",
                    f"POSTGRES_USER={POSTGRES_USER}",
                    "-e",
                    f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
                    "-e",
                    f"POSTGRES_DB={POSTGRES_DB}",
                    IMAGE,
                ]
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to create {CONTAINER_NAME} container.\n"
                f"Error: {e.stderr}\n"
                f"This might be due to port {PORT} already being in use."
            ) from e
        console.print(f"[green]Created {CONTAINER_NAME} container[/green]")

        # Wait for Postgres to be ready (first time takes longer)
        console.print("[dim]Waiting for Postgres to start...[/dim]")
        if not _wait_for_postgres(timeout=60):
            raise RuntimeError("Postgres failed to start within 60 seconds")
        console.print("[green]Postgres is ready[/green]")

    elif not _container_running():
        console.print(f"[dim]Starting {CONTAINER_NAME} container...[/dim]")
        _run_docker(["start", CONTAINER_NAME])

        # Wait for Postgres to be ready
        if not _wait_for_postgres(timeout=30):
            raise RuntimeError("Postgres failed to start within 30 seconds")
        console.print("[green]Postgres is ready[/green]")

    return f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@localhost:{PORT}/{POSTGRES_DB}"


def stop_postgres() -> None:
    """Stop the Postgres container."""
    if _container_exists() and _container_running():
        console.print(f"[dim]Stopping {CONTAINER_NAME} container...[/dim]")
        _run_docker(["stop", CONTAINER_NAME])
        console.print(f"[green]Stopped {CONTAINER_NAME}[/green]")
    else:
        console.print(f"[dim]{CONTAINER_NAME} is not running[/dim]")


def reset_postgres() -> str:
    """Reset the Postgres container (delete and recreate).

    Returns:
        Connection URL for the new Postgres database.
    """
    if _container_exists():
        console.print(f"[dim]Removing {CONTAINER_NAME} container...[/dim]")
        if _container_running():
            _run_docker(["stop", CONTAINER_NAME])
        _run_docker(["rm", CONTAINER_NAME])

        # Also remove the volume to get a fresh database
        console.print(f"[dim]Removing {VOLUME_NAME} volume...[/dim]")
        _run_docker(["volume", "rm", VOLUME_NAME], check=False)

        console.print(f"[green]Removed {CONTAINER_NAME} and its data[/green]")

    return ensure_postgres()
