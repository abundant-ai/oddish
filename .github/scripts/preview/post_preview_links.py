"""Create or update the sticky PR comment with preview environment links."""

import json
import os
import urllib.error
import urllib.request

MARKER = "<!-- oddish-preview-links -->"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def gh_request(method: str, path: str, body: dict | None = None):
    token = env("GITHUB_TOKEN")
    repo = env("GITHUB_REPOSITORY")
    url = f"https://api.github.com/repos/{repo}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            if response.status == 204:
                return None
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} failed: {detail}") from exc


def link_or_text(label: str, url: str) -> str:
    return f"[{label}]({url})" if url else label


def build_body() -> str:
    commit_sha = env("PREVIEW_COMMIT_SHA")
    short_sha = commit_sha[:7] if commit_sha else "unknown"
    vercel_url = env("VERCEL_PREVIEW_URL")
    vercel_deployment_url = env("VERCEL_DEPLOYMENT_URL")
    modal_api_url = env("MODAL_API_URL")
    backend_label = env("PREVIEW_BACKEND_LABEL", "unknown")
    database_label = env("PREVIEW_DATABASE_LABEL", "unknown")
    supabase_project_ref = env("SUPABASE_PROJECT_REF")
    supabase_branch_ref = env("SUPABASE_BRANCH_REF")
    deploy_backend = env("DEPLOY_BACKEND", "false")
    run_migrations = env("RUN_MIGRATIONS", "false")
    deploy_frontend = env("DEPLOY_FRONTEND", "false")

    supabase_url = ""
    if supabase_project_ref and supabase_branch_ref:
        supabase_url = (
            f"https://supabase.com/dashboard/project/{supabase_project_ref}/branches"
        )

    rows = [
        f"| Frontend | {link_or_text(vercel_url or 'not deployed', vercel_url)} | Vercel preview for `{short_sha}` |",
        f"| Backend | {link_or_text(backend_label, modal_api_url)} | `{backend_label}` |",
        f"| Database | {link_or_text(database_label, supabase_url)} | `{database_label}` |",
    ]

    return "\n".join(
        [
            MARKER,
            "## Oddish preview",
            "",
            f"Commit: `{commit_sha or 'unknown'}`",
            "",
            "| Surface | Link | Target |",
            "| --- | --- | --- |",
            *rows,
            "",
            *(
                [f"Vercel deployment URL: {vercel_deployment_url}", ""]
                if vercel_deployment_url and vercel_deployment_url != vercel_url
                else []
            ),
            "Plan:",
            f"- Frontend deploy: `{deploy_frontend}`",
            f"- Backend deploy: `{deploy_backend}`",
            f"- Migrations: `{run_migrations}`",
            "",
            "_This comment is updated by the PR Preview workflow._",
        ]
    )


def main() -> None:
    pr_number = env("PR_NUMBER")
    body = build_body()
    comments = gh_request("GET", f"/issues/{pr_number}/comments?per_page=100")
    existing = next(
        (comment for comment in comments if MARKER in comment.get("body", "")),
        None,
    )

    if existing:
        gh_request("PATCH", f"/issues/comments/{existing['id']}", {"body": body})
    else:
        gh_request("POST", f"/issues/{pr_number}/comments", {"body": body})


if __name__ == "__main__":
    main()
