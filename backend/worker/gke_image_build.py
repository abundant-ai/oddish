"""Upload-time GKE task-image builder (the primary build path).

When a submission classifies onto the GKE variant, the API spawns
``build_gke_task_image`` right after the task rows commit, so the
content-addressed image is usually READY before a worker claims the first
trial. The worker-side ``auto_build_missing_image`` fallback covers the race
where a trial starts before this build finishes (both paths build the same
content-addressed tag, so a duplicate build is wasteful but harmless).

Runs on the GKE variant worker image: name/tag derivation calls the same
harbor code the trial-time environment uses, so parity is structural rather
than reimplemented.
"""

from __future__ import annotations

import io
import tarfile
import tempfile
import tomllib
from pathlib import Path

from oddish.config import settings
from oddish.db.storage import StorageClient


def _task_short_name(task_toml: dict) -> str:
    """Mirror harbor TaskConfig.short_name (name without the org prefix)."""
    name = (task_toml.get("task") or {}).get("name") or ""
    if "/" in name:
        return name.split("/")[1]
    return name


def _content_hash(env_dir: Path, docker_image: str | None) -> str:
    from harbor.environments.definition import environment_content_hash

    return environment_content_hash(env_dir, docker_image=docker_image)


def _image_exists(*, image: str, tag: str) -> bool:
    from harbor.environments import gke_auth

    return gke_auth.artifact_tag_exists(
        project_id=settings.gke_project_id,
        location=settings.gke_registry_location,
        repository=settings.gke_registry_name,
        image=image,
        tag=tag,
    )


def _run_cloud_build(*, context_dir: str, image_url: str) -> None:
    from harbor.environments import gke_auth

    gke_auth.build_image_via_cloud_build(
        project_id=settings.gke_project_id,
        region=settings.gke_region,
        context_dir=context_dir,
        image_url=image_url,
    )


async def _fetch_task_archive(task_id: str, version: int) -> bytes:
    storage = StorageClient()
    _root, archive_key = await storage._resolve_task_prefix(task_id, version)
    return await storage.download_bytes(archive_key)


def _extract_archive(archive_bytes: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")


async def ensure_task_image(task_id: str, version: int) -> str:
    """Build the task's content-addressed GKE image unless it already exists.

    Returns a short outcome marker (for logs/tests): ``no-gke-config``,
    ``task-docker-image``, ``exists``, or ``built <image_url>``.
    """
    if not (
        settings.gke_project_id
        and settings.gke_registry_location
        and settings.gke_registry_name
    ):
        return "no-gke-config"

    archive_bytes = await _fetch_task_archive(task_id, version)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _extract_archive(archive_bytes, root)

        task_toml_path = root / "task.toml"
        task_toml = tomllib.loads(task_toml_path.read_text()) if task_toml_path.exists() else {}
        docker_image = (task_toml.get("environment") or {}).get("docker_image")
        if docker_image:
            # The pod uses the task-provided image verbatim; nothing to build.
            return "task-docker-image"

        env_dir = root / "environment"
        if not env_dir.is_dir():
            return "no-environment-dir"

        image_name = _task_short_name(task_toml) or task_id
        tag = _content_hash(env_dir, docker_image=None)
        image_url = (
            f"{settings.gke_registry_location}-docker.pkg.dev/"
            f"{settings.gke_project_id}/{settings.gke_registry_name}/"
            f"{image_name}:{tag}"
        )

        if _image_exists(image=image_name, tag=tag):
            return "exists"

        _run_cloud_build(context_dir=str(env_dir), image_url=image_url)
        return f"built {image_url}"
