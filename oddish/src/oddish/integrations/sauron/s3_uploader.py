"""
Mirror trial artifacts to sauron's AWS S3 bucket.

When ODDISH_SAURON_S3_BUCKET is set, trial results are uploaded to
sauron's bucket so sauron can render oddish experiments using its
existing UI components.

Layout:
    PR-triggered:
        {owner}/{repo}/pr-{n}/run-{experiment_id}/
            run-meta.json
            agent-{name}:{model}/{task_name}/attempt_{n}/...

    CLI-triggered:
        {org_slug}/runs/{experiment_id}/run-{experiment_id}/
            run-meta.json
            agent-{name}:{model}/{task_name}/attempt_{n}/...

The CLI path uses the experiment_id literally as the grouping segment
(not a synthetic pr-N) so sauron's existing 4-segment route renders it
without modification. `run-meta.json` carries identity/metadata at the
run root so sauron can render run headers without parsing the path or
hitting GitHub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import aioboto3
from botocore.config import Config

from oddish.config import settings
from oddish.integrations.github.client import GitHubMeta

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1


class SauronS3Uploader:
    """Best-effort mirror of trial artifacts to sauron's AWS S3 bucket."""

    def __init__(self) -> None:
        self._client: aioboto3.Client | None = None
        self._session: aioboto3.Session | None = None

    @property
    def _s3(self) -> aioboto3.Client:
        # Narrow the optional attribute for callers that have already
        # awaited ``_ensure_client``. Mirrors ``db.storage.StorageClient._s3``.
        assert self._client is not None, "call _ensure_client() first"
        return self._client  # type: ignore[return-value]

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
        assert self._session is not None
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
        experiment_name: str | None,
        attempt_number: int,
        github_meta: GitHubMeta | None,
        task_tags: dict | None = None,
    ) -> str | None:
        """Upload trial artifacts. Returns the trial S3 prefix or None on failure."""
        if not self.is_enabled():
            return None

        run_prefix = self._build_run_prefix(
            github_meta=github_meta, experiment_id=experiment_id
        )
        attempt_prefix = (
            f"{run_prefix}agent-{agent}:{(model or 'default').replace('/', '-')}/"
            f"{task_name}/attempt_{attempt_number}/"
        )

        # Harbor's job_dir contains a task-{name}__{hash}/ subdirectory with
        # the actual trial output. Sauron expects these at the attempt root.
        source = self._find_trial_subdir(harbor_job_dir) or harbor_job_dir

        try:
            await self._upload_directory(source, attempt_prefix)
            await self._write_manifest(
                run_prefix=run_prefix,
                experiment_id=experiment_id,
                experiment_name=experiment_name,
                github_meta=github_meta,
                task_tags=task_tags,
            )
            return attempt_prefix
        except Exception as e:
            logger.warning("Sauron mirror failed for %s: %s", attempt_prefix, e)
            return None

    # -- Path construction ---------------------------------------------------

    @staticmethod
    def _build_run_prefix(*, github_meta: GitHubMeta | None, experiment_id: str) -> str:
        if github_meta:
            return (
                f"{github_meta.owner}/{github_meta.repo}/"
                f"pr-{github_meta.pr_number}/run-{experiment_id}/"
            )
        org = settings.sauron_s3_org or "oddish"
        # 4-segment path so sauron's existing [org]/[repo]/[pr]/[run] route
        # renders without modification. The experiment_id appears twice:
        # once as the grouping ("pr") segment, once as the run identifier.
        return f"{org}/runs/{experiment_id}/run-{experiment_id}/"

    # -- Manifest ------------------------------------------------------------

    async def _write_manifest(
        self,
        *,
        run_prefix: str,
        experiment_id: str,
        experiment_name: str | None,
        github_meta: GitHubMeta | None,
        task_tags: dict | None,
    ) -> None:
        """Write run-meta.json at run root. Last-writer-wins for stable fields."""
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "kind": "pr" if github_meta else "experiment",
            "experiment_id": experiment_id,
            "experiment_name": experiment_name,
            "github": (
                {
                    "owner": github_meta.owner,
                    "repo": github_meta.repo,
                    "pr_number": github_meta.pr_number,
                }
                if github_meta
                else None
            ),
            "tags": {k: v for k, v in (task_tags or {}).items() if k != "github_meta"},
        }
        body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

        await self._ensure_client()
        await self._s3.put_object(
            Bucket=settings.sauron_s3_bucket,
            Key=f"{run_prefix}run-meta.json",
            Body=body,
            ContentType="application/json",
        )

    # -- Harbor directory unwrapping -----------------------------------------

    @staticmethod
    def _find_trial_subdir(harbor_job_dir: Path) -> Path | None:
        """Find the trial subdirectory (task-name__hash/) inside job_dir."""
        if not harbor_job_dir.exists():
            return None
        subdirs = [d for d in harbor_job_dir.iterdir() if d.is_dir()]
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
                await self._s3.upload_file(str(f), settings.sauron_s3_bucket, key)

        await asyncio.gather(*(upload_one(f) for f in files), return_exceptions=True)


# -- Singleton ---------------------------------------------------------------

_uploader: SauronS3Uploader | None = None


def get_sauron_uploader() -> SauronS3Uploader:
    global _uploader
    if _uploader is None:
        _uploader = SauronS3Uploader()
    return _uploader
