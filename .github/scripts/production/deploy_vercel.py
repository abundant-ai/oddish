"""Create the production Vercel deployment for an exact commit and wait for it.

Why the trigger lives here instead of Vercel's git integration: see the header
of .github/workflows/modal-deploy.yml. frontend/vercel.json disables git
deploys for main, and the Production Deploy workflow runs this script after
the backend job, so a new frontend never talks to an old backend. The
deployment is created from the connected repository at VERCEL_GIT_COMMIT_SHA —
the same build pipeline and project settings as the git-triggered deploys it
replaces; only the trigger moved.

Exits non-zero unless the deployment reaches READY and the production alias is
assigned. With git deploys off, a red job here is the only signal that
production is still serving the previous frontend — the polling half of this
script is load-bearing, not ceremony.

Inputs (env vars):
  VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID,
  VERCEL_GIT_BRANCH, VERCEL_GIT_COMMIT_SHA,
  GITHUB_RUN_ATTEMPT, GITHUB_STEP_SUMMARY (optional)
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

READY_TIMEOUT_S = 25 * 60
ALIAS_TIMEOUT_S = 3 * 60
POLL_INTERVAL_S = 15
REQUEST_TIMEOUT_S = 30
# Transient API failures tolerated in a row while polling (~100 GETs over 25
# minutes will see the odd 5xx/429); sustained failure still fails the job.
MAX_CONSECUTIVE_POLL_FAILURES = 8
# BLOCKED (spend limit / abuse review) is terminal for this run: waiting the
# full budget on it would only delay the red job.
FAILURE_STATES = {"ERROR", "CANCELED", "DELETED", "BLOCKED"}


class VercelApiError(Exception):
    pass


def api(method, path, body=None, query=None):
    params = {"teamId": os.environ["VERCEL_ORG_ID"], **(query or {})}
    request = urllib.request.Request(
        f"https://api.vercel.com{path}?{urllib.parse.urlencode(params)}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['VERCEL_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise VercelApiError(
            f"{method} {path} failed with {error.code}: {detail}"
        ) from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise VercelApiError(f"{method} {path} failed: {error}") from error


def main():
    branch = os.environ["VERCEL_GIT_BRANCH"]
    commit_sha = os.environ["VERCEL_GIT_COMMIT_SHA"]

    project = api("GET", f"/v9/projects/{os.environ['VERCEL_PROJECT_ID']}")
    link = project.get("link") or {}
    if link.get("type") != "github" or not link.get("repoId"):
        raise SystemExit(
            "The Vercel project is not linked to a GitHub repository, so a "
            "production deployment cannot be created from a commit."
        )

    # First attempt: no forceNew, so re-invoking for a sha Vercel already
    # built returns that deployment instead of building twice (unlike
    # preview/redeploy_vercel.py, which forces a rebuild on purpose). A
    # workflow RE-RUN forces a fresh build — otherwise deduplication would
    # keep handing back the failed deployment and no re-run could ever pass.
    rerun = os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1"
    deployment = api(
        "POST",
        "/v13/deployments",
        body={
            "name": project["name"],
            "target": "production",
            "gitSource": {
                "type": "github",
                "repoId": link["repoId"],
                "ref": branch,
                "sha": commit_sha,
            },
        },
        query={"forceNew": "1"} if rerun else None,
    )
    # Deduplication may return an existing deployment. Only a production one
    # proves anything: a preview build of this same sha (every promoted sha
    # was the staging tip once) is READY with its branch alias assigned and
    # would false-green both polls below.
    if deployment.get("target") != "production":
        raise SystemExit(
            f"Vercel returned a non-production deployment "
            f"(target={deployment.get('target')!r}) for {commit_sha}; "
            "refusing to treat it as the production deploy."
        )
    deployment_id = deployment["id"]
    url = "https://" + deployment["url"]
    print(f"Production deployment {deployment_id} for {commit_sha}: {url}")

    def poll(condition, timeout_s, describe):
        deadline = time.monotonic() + timeout_s
        failures = 0
        while True:
            try:
                current = api("GET", f"/v13/deployments/{deployment_id}")
                failures = 0
            except VercelApiError as error:
                failures += 1
                if failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                    raise SystemExit(
                        f"Polling deployment {deployment_id} failed "
                        f"{failures} times in a row: {error}"
                    ) from error
            else:
                state = current.get("readyState", "UNKNOWN")
                if state in FAILURE_STATES:
                    raise SystemExit(f"Deployment {deployment_id} ended {state}.")
                if current.get("aliasError"):
                    raise SystemExit(
                        f"Production alias failed: {current['aliasError']}"
                    )
                if condition(current):
                    return current
                print(f"Deployment {deployment_id}: {state}, waiting for {describe}")
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"Deployment {deployment_id} still not {describe} after "
                    f"{timeout_s // 60} minutes."
                )
            time.sleep(POLL_INTERVAL_S)

    poll(lambda d: d.get("readyState") == "READY", READY_TIMEOUT_S, "READY")
    # READY means the build succeeded; the production domain flips when the
    # alias is assigned, and that flip is what "the frontend shipped" means.
    final = poll(
        lambda d: bool(d.get("aliasAssigned")), ALIAS_TIMEOUT_S, "alias assignment"
    )

    aliases = final.get("alias") or []
    production_url = f"https://{aliases[0]}" if aliases else url
    print(f"Production frontend is live: {production_url}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(
                "## Vercel production deployment\n\n"
                f"- Production: {production_url}\n"
                f"- Deployment: {url}\n"
                f"- Commit: `{commit_sha}`\n"
            )


if __name__ == "__main__":
    try:
        main()
    except VercelApiError as error:
        raise SystemExit(f"Vercel API error: {error}")
