"""
Sauron S3 mirror — uploads trial results to sauron's AWS S3 bucket.

Sauron's frontend reads from `s3://abundant-github-workflows-bucket/{org}/{repo}/pr-{n}/run-{id}/...`
in a specific layout produced by task-workflows GitHub Actions. This module
mirrors oddish-executed trial artifacts into that same layout, so sauron can
render oddish experiments using its existing components, renderers, and
trajectory viewer.

Disabled when `ODDISH_SAURON_S3_BUCKET` is empty.

Layout written:
    {org}/{repo}/pr-{n}/run-{run_id}/
        experiment-manifest.yaml          (uploaded once per experiment)
        agent-{name}:{model_key}/
            {task_name}/
                attempt_{n}/
                    result.json
                    agent/trajectory.json
                    verifier/reward.txt
                    verifier/test-stdout.txt
                    task/instruction.md (best-effort)
                    ...

Where:
    - org/repo come from task.tags["github_meta"] when present (PR-triggered runs),
      otherwise fall back to ODDISH_SAURON_S3_ORG/REPO settings.
    - pr-{n} is the GitHub PR number, or `pr-0` for non-PR (CLI) runs.
    - run-{run_id} is the oddish experiment_id.
    - agent-{name}:{model_key} mirrors task-workflows convention, with `/` in the
      model name replaced by `-`.
    - The trial subdirectory inside Harbor's job_dir (named `{task}__{hash}/`)
      is unwrapped — its contents are uploaded directly into attempt_{n}/.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from oddish.config import settings
from oddish.integrations.github.client import GitHubMeta

logger = logging.getLogger(__name__)

_MAX_CONCURRENT_UPLOADS = 16
_TASK_ARCHIVE_NAME = ".oddish-task.tar.gz"


def _model_key(model: str | None) -> str:
    """Sauron's convention: replace '/' in model name with '-'."""
    if not model:
        return "default"
    return model.replace("/", "-")


def _agent_folder(agent: str, model: str | None) -> str:
    """Build the `agent-{name}:{model_key}` folder name."""
    return f"agent-{agent}:{_model_key(model)}"


@dataclass
class SauronUploadContext:
    """Inputs needed to upload a single trial to sauron's S3 bucket."""

    trial_id: str
    harbor_job_dir: Path
    task_id: str
    task_name: str
    agent: str
    model: str | None
    experiment_id: str
    experiment_name: str
    attempt_number: int  # 1-indexed
    n_trials: int  # for the manifest
    github_meta: GitHubMeta | None
    task_source_path: Path | None  # for task/ files (instruction.md etc.)


class SauronS3Uploader:
    """Mirror trial artifacts to sauron's AWS S3 bucket."""

    def __init__(self) -> None:
        self._client: aioboto3.Client | None = None
        self._session: aioboto3.Session | None = None

    def is_enabled(self) -> bool:
        return bool(
            settings.sauron_s3_bucket
            and os.environ.get("AWS_ACCESS_KEY_ID")
            and os.environ.get("AWS_SECRET_ACCESS_KEY")
        )

    async def _ensure_client(self) -> None:
        if self._client is not None:
            return
        self._session = aioboto3.Session()
        self._client = await self._session.client(
            "s3",
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
            config=Config(signature_version="s3v4"),
        ).__aenter__()

    async def close(self) -> None:
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None

    # ------------------------------------------------------------------------
    # Path construction
    # ------------------------------------------------------------------------

    def _run_prefix(
        self,
        *,
        github_meta: GitHubMeta | None,
        experiment_id: str,
    ) -> str:
        """`{org}/{repo}/pr-{n}/run-{experiment_id}/`"""
        if github_meta:
            org = github_meta.owner
            repo = github_meta.repo
            pr_number = github_meta.pr_number
        else:
            org = settings.sauron_s3_org or "oddish"
            repo = settings.sauron_s3_repo or "cli-runs"
            pr_number = 0
        return f"{org}/{repo}/pr-{pr_number}/run-{experiment_id}/"

    def _attempt_prefix(self, ctx: SauronUploadContext) -> str:
        run_prefix = self._run_prefix(
            github_meta=ctx.github_meta, experiment_id=ctx.experiment_id
        )
        agent_folder = _agent_folder(ctx.agent, ctx.model)
        return (
            f"{run_prefix}{agent_folder}/{ctx.task_name}/"
            f"attempt_{ctx.attempt_number}/"
        )

    # ------------------------------------------------------------------------
    # S3 helpers
    # ------------------------------------------------------------------------

    async def _put_object(self, key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
        await self._ensure_client()
        await self._client.put_object(  # type: ignore[union-attr]
            Bucket=settings.sauron_s3_bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    async def _upload_file(self, local_path: Path, key: str) -> None:
        await self._ensure_client()
        await self._client.upload_file(  # type: ignore[union-attr]
            str(local_path), settings.sauron_s3_bucket, key
        )

    async def _object_exists(self, key: str) -> bool:
        await self._ensure_client()
        try:
            await self._client.head_object(  # type: ignore[union-attr]
                Bucket=settings.sauron_s3_bucket, Key=key
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    # ------------------------------------------------------------------------
    # Harbor job_dir unwrapping
    # ------------------------------------------------------------------------

    def _find_trial_subdir(self, harbor_job_dir: Path) -> Path | None:
        """Find the single trial subdirectory inside Harbor's job_dir.

        Harbor names trial dirs like `{task_name}__{random_hash}`. If exactly
        one such subdir exists, return it. Otherwise fall back to a single-subdir
        check, or None if the structure is unexpected.
        """
        if not harbor_job_dir.exists():
            return None

        subdirs = [d for d in harbor_job_dir.iterdir() if d.is_dir()]
        # Prefer dirs matching Harbor's `{name}__{hash}` convention
        trial_dirs = [d for d in subdirs if "__" in d.name]
        if len(trial_dirs) == 1:
            return trial_dirs[0]
        if len(subdirs) == 1:
            return subdirs[0]
        return None

    async def _upload_directory(self, local_dir: Path, s3_prefix: str) -> None:
        """Upload all files under local_dir to s3_prefix, with bounded concurrency."""
        files = [p for p in local_dir.rglob("*") if p.is_file()]
        if not files:
            return

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_UPLOADS)

        async def upload_one(file_path: Path) -> None:
            relative = file_path.relative_to(local_dir).as_posix()
            key = f"{s3_prefix}{relative}"
            async with semaphore:
                try:
                    await self._upload_file(file_path, key)
                except Exception as e:
                    logger.warning(
                        "Sauron mirror: failed to upload %s -> %s: %s",
                        file_path,
                        key,
                        e,
                    )

        await asyncio.gather(*(upload_one(f) for f in files))

    # ------------------------------------------------------------------------
    # Task definition files (best-effort)
    # ------------------------------------------------------------------------

    async def _upload_task_files(
        self, task_source_path: Path | None, s3_prefix: str
    ) -> None:
        """Upload task definition files (instruction.md, etc.) to {prefix}task/."""
        if not task_source_path or not task_source_path.exists():
            return

        # Files we want to surface in sauron's Task tab.
        # Match what sauron reads via getTaskFilesFromS3.
        wanted = {
            "instruction.md",
            "task.toml",
            "README.md",
            "README",
        }

        for name in wanted:
            candidate = task_source_path / name
            if candidate.is_file():
                key = f"{s3_prefix}task/{name}"
                try:
                    await self._upload_file(candidate, key)
                except Exception as e:
                    logger.debug("Sauron mirror: skip task file %s: %s", name, e)

        # Also upload the solution/ and tests/ subdirectories if they exist.
        for subdir_name in ("solution", "tests"):
            subdir = task_source_path / subdir_name
            if subdir.is_dir():
                await self._upload_directory(subdir, f"{s3_prefix}task/{subdir_name}/")

    # ------------------------------------------------------------------------
    # experiment-manifest.yaml
    # ------------------------------------------------------------------------

    async def ensure_experiment_manifest(
        self,
        *,
        experiment_id: str,
        experiment_name: str,
        github_meta: GitHubMeta | None,
        tasks: list[str],
        agents: list[dict[str, Any]],
        n_trials: int,
    ) -> None:
        """Write experiment-manifest.yaml to S3 if it doesn't already exist."""
        if not self.is_enabled():
            return

        prefix = self._run_prefix(
            github_meta=github_meta, experiment_id=experiment_id
        )
        manifest_key = f"{prefix}experiment-manifest.yaml"

        try:
            if await self._object_exists(manifest_key):
                return
        except Exception as e:
            logger.debug("Sauron mirror: head_object failed for %s: %s", manifest_key, e)
            # Fall through and try to upload anyway

        yaml_content = self._build_manifest_yaml(
            name=experiment_name,
            tasks=tasks,
            agents=agents,
            n_trials=n_trials,
        )
        try:
            await self._put_object(
                manifest_key, yaml_content.encode("utf-8"), "application/x-yaml"
            )
        except Exception as e:
            logger.warning(
                "Sauron mirror: failed to upload experiment-manifest.yaml: %s", e
            )

    def _build_manifest_yaml(
        self,
        *,
        name: str,
        tasks: list[str],
        agents: list[dict[str, Any]],
        n_trials: int,
    ) -> str:
        """Build a sauron-compatible experiment-manifest.yaml."""
        lines = [
            'version: "1.0"',
            "metadata:",
            f"  name: {self._yaml_quote(name)}",
            f"  n_trials: {n_trials}",
            "tasks:",
        ]
        for task in tasks:
            lines.append(f"  - {self._yaml_quote(task)}")
        lines.append("agents:")
        for agent_spec in agents:
            agent_name = agent_spec.get("name", "")
            model = agent_spec.get("model")
            if model:
                lines.append(f"  - name: {self._yaml_quote(agent_name)}")
                lines.append(f"    model_name: {self._yaml_quote(model)}")
            else:
                lines.append(f"  - {self._yaml_quote(agent_name)}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _yaml_quote(value: str) -> str:
        """Conservative YAML string quoting."""
        if not value:
            return '""'
        if re.match(r'^[A-Za-z0-9_\-./]+$', value):
            return value
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'

    # ------------------------------------------------------------------------
    # Entrypoint
    # ------------------------------------------------------------------------

    async def upload_trial_to_sauron(self, ctx: SauronUploadContext) -> str | None:
        """Upload a single trial's artifacts to sauron's S3 bucket.

        Returns the sauron S3 prefix on success, or None if disabled/failed.
        """
        if not self.is_enabled():
            return None

        attempt_prefix = self._attempt_prefix(ctx)

        # Find Harbor's trial subdirectory and unwrap it.
        trial_subdir = self._find_trial_subdir(ctx.harbor_job_dir)
        source_dir = trial_subdir if trial_subdir else ctx.harbor_job_dir

        try:
            await self._upload_directory(source_dir, attempt_prefix)
        except Exception as e:
            logger.warning(
                "Sauron mirror: failed to upload trial %s artifacts: %s",
                ctx.trial_id,
                e,
            )
            return None

        # Best-effort: upload task definition files into {attempt_prefix}task/
        try:
            await self._upload_task_files(ctx.task_source_path, attempt_prefix)
        except Exception as e:
            logger.debug(
                "Sauron mirror: skipped task files for %s: %s", ctx.trial_id, e
            )

        return attempt_prefix


# ----------------------------------------------------------------------------
# Module-level singleton
# ----------------------------------------------------------------------------

_uploader: SauronS3Uploader | None = None


def get_sauron_uploader() -> SauronS3Uploader:
    """Return a process-wide SauronS3Uploader (lazy-initialized)."""
    global _uploader
    if _uploader is None:
        _uploader = SauronS3Uploader()
    return _uploader
