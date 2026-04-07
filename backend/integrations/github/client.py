"""Re-export from oddish — GitHub client now lives in the core package."""

from oddish.integrations.github.client import (  # noqa: F401
    GitHubMeta,
    GitHubClient,
    get_github_client,
    GITHUB_API_BASE,
)
