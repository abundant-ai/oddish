"""
GitHub API client for PR comment updates.

Supports both Personal Access Tokens (GITHUB_TOKEN) and GitHub App credentials.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


@dataclass
class GitHubMeta:
    """Parsed GitHub metadata from task tags."""

    owner: str
    repo: str
    pr_number: int
    pr_url: str | None = None
    pr_title: str | None = None

    @classmethod
    def from_tags(cls, tags: dict | None) -> "GitHubMeta | None":
        """Parse github_meta from task tags. Returns None if not a GitHub PR task."""
        if not tags:
            return None

        raw = tags.get("github_meta")
        if not raw:
            return None

        # Handle both string (JSON) and dict formats
        import json

        if isinstance(raw, str):
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, dict):
            meta = raw
        else:
            return None

        # Extract required fields
        pr_number = meta.get("pr_number")
        pr_repo = meta.get("pr_repo")

        if not pr_number or not pr_repo:
            return None

        # Parse owner/repo from pr_repo (e.g., "abundant-ai/harbor-forge")
        parts = pr_repo.split("/")
        if len(parts) != 2:
            return None

        try:
            return cls(
                owner=parts[0],
                repo=parts[1],
                pr_number=int(pr_number),
                pr_url=meta.get("pr_url"),
                pr_title=meta.get("pr_title"),
            )
        except (ValueError, TypeError):
            return None


class GitHubClient:
    """Async GitHub API client for PR operations."""

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._client = httpx.AsyncClient(
                base_url=GITHUB_API_BASE,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def find_oddish_comment(
        self, owner: str, repo: str, pr_number: int
    ) -> dict[str, Any] | None:
        """Find existing Oddish comment on a PR.

        Looks for comments with any of the Oddish markers.
        """
        client = await self._get_client()
        try:
            response = await client.get(
                f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
                params={"per_page": 100},
            )
            response.raise_for_status()

            # Check for any Oddish marker (validation or experiment)
            markers = [
                "<!-- oddish-validation-results -->",
                "<!-- oddish-experiment-results -->",
            ]
            for comment in response.json():
                body = comment.get("body", "")
                if any(marker in body for marker in markers):
                    return comment
            return None

        except httpx.HTTPStatusError as e:
            logger.warning(f"Failed to fetch PR comments: {e}")
            return None

    async def create_comment(
        self, owner: str, repo: str, pr_number: int, body: str
    ) -> dict[str, Any] | None:
        """Create a new comment on a PR."""
        client = await self._get_client()
        try:
            response = await client.post(
                f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
                json={"body": body},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to create PR comment: {e}")
            return None

    async def update_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> dict[str, Any] | None:
        """Update an existing comment."""
        client = await self._get_client()
        try:
            response = await client.patch(
                f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
                json={"body": body},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to update PR comment: {e}")
            return None

    async def upsert_oddish_comment(
        self, owner: str, repo: str, pr_number: int, body: str
    ) -> dict[str, Any] | None:
        """Create or update the Oddish comment on a PR."""
        existing = await self.find_oddish_comment(owner, repo, pr_number)
        if existing:
            return await self.update_comment(owner, repo, existing["id"], body)
        return await self.create_comment(owner, repo, pr_number, body)


# Singleton client instance
_client: GitHubClient | None = None


def get_github_client() -> GitHubClient:
    """Get or create the singleton GitHub client."""
    global _client
    if _client is None:
        _client = GitHubClient()
    return _client
