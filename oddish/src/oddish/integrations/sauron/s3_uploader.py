"""
Mirror trial artifacts to sauron's AWS S3 bucket.

When ODDISH_SAURON_S3_BUCKET is set, trial results are uploaded to
sauron's bucket in sauron's expected layout so sauron can render
oddish experiments using its existing UI components.

Layout:
    {org}/{repo}/pr-{n}/run-{experiment_id}/
        agent-{name}:{model}/
            {task_name}/
                attempt_{n}/
                    result.json, agent/trajectory.json, verifier/reward.txt, ...
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import aioboto3
from botocore.config import Config

from oddish.config import settings
from oddish.integrations.github.client import GitHubMeta

logger = logging.getLogger(__name__)


class SauronS3Uploader:
    """Best-effort mirror of trial artifacts to sauron's AWS S3 bucket."""

    def __init__(self) -> None:
        self._client = None
        self._session = None

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

    async def upload_trial(
        self,
        *,
        harbor_job_dir: Path,
        task_name: str,
        agent: str,
        model: str | None,
        experiment_id: str,
        attempt_number: int,
        github_meta: GitHubMeta | None,
    ) -> str | None:
        """Upload trial artifacts. Returns the S3 prefix or None on failure."""
        if not self.is_enabled():
            return None

        prefix = self._build_prefix(
            github_meta=github_meta,
            experiment_id=experiment_id,
            agent=agent,
            model=model,
            task_name=task_name,
            attempt_number=attempt_number,
        )

        # Harbor's job_dir contains a task-{name}__{hash}/ subdirectory with
        # the actual trial output (agent/, verifier/, result.json). Sauron
        # expects these at the attempt root, so we upload from the subdirectory.
        source = self._find_trial_subdir(harbor_job_dir) or harbor_job_dir

        try:
            await self._upload_directory(source, prefix)
            return prefix
        except Exception as e:
            logger.warning("Sauron mirror failed for %s: %s", prefix, e)
            return None

    # -- Path construction ---------------------------------------------------

    def _build_prefix(
        self,
        *,
        github_meta: GitHubMeta | None,
        experiment_id: str,
        agent: str,
        model: str | None,
        task_name: str,
        attempt_number: int,
    ) -> str:
        if github_meta:
            org, repo, pr = github_meta.owner, github_meta.repo, github_meta.pr_number
        else:
            org = settings.sauron_s3_org or "oddish"
            repo = settings.sauron_s3_repo or "cli-runs"
            pr = 0

        model_key = (model or "default").replace("/", "-")
        return (
            f"{org}/{repo}/pr-{pr}/run-{experiment_id}/"
            f"agent-{agent}:{model_key}/{task_name}/"
            f"attempt_{attempt_number}/"
        )

    # -- Harbor directory unwrapping -----------------------------------------

    @staticmethod
    def _find_trial_subdir(harbor_job_dir: Path) -> Path | None:
        """Find the trial subdirectory (task-name__hash/) inside job_dir."""
        if not harbor_job_dir.exists():
            return None
        subdirs = [d for d in harbor_job_dir.iterdir() if d.is_dir()]
        # Prefer dirs matching Harbor's {name}__{hash} convention
        trial_dirs = [d for d in subdirs if "__" in d.name]
        if len(trial_dirs) == 1:
            return trial_dirs[0]
        if len(subdirs) == 1:
            return subdirs[0]
        return None

    # -- S3 upload -----------------------------------------------------------

    async def _upload_directory(self, local_dir: Path, s3_prefix: str) -> None:
        files = [p for p in local_dir.rglob("*") if p.is_file()]
        if not files:
            return

        sem = asyncio.Semaphore(16)

        async def upload_one(f: Path) -> None:
            key = f"{s3_prefix}{f.relative_to(local_dir).as_posix()}"
            async with sem:
                await self._ensure_client()
                await self._client.upload_file(str(f), settings.sauron_s3_bucket, key)

        await asyncio.gather(*(upload_one(f) for f in files), return_exceptions=True)


# -- Singleton ---------------------------------------------------------------

_uploader: SauronS3Uploader | None = None


def get_sauron_uploader() -> SauronS3Uploader:
    global _uploader
    if _uploader is None:
        _uploader = SauronS3Uploader()
    return _uploader
