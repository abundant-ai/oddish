"""Create the production Vercel deployment for an exact commit and wait for it.

Why the trigger lives here instead of Vercel's git integration: see the header
of .github/workflows/modal-deploy.yml. frontend/vercel.json disables git
deploys for main, and the Production Deploy workflow runs this script after
the backend job, so a new frontend never talks to an old backend. The
deployment is created from the connected repository at VERCEL_GIT_COMMIT_SHA —
the same build pipeline and project settings as the git-triggered deploys it
replaces; only the trigger moved.

Exits non-zero unless the deployment reaches READY, the production alias is
assigned, and this deployment currently owns production. With git deploys off,
a red job here is the only signal that production is still serving the
previous frontend — the polling half of this script is load-bearing, not
ceremony. Superseded runs refuse to act at all: a re-run of an old workflow
run must neither report green for a commit production no longer serves nor
rebuild that old commit and hand the domain back to it.

Inputs (env vars):
  VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID,
  VERCEL_GIT_BRANCH, VERCEL_GIT_COMMIT_SHA,
  GITHUB_STEP_SUMMARY (optional)
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

# The frontend job's timeout-minutes in modal-deploy.yml must stay comfortably
# above READY_TIMEOUT_S + ALIAS_TIMEOUT_S plus per-request overhead, so a slow
# build fails here with a diagnostic instead of as an opaque runner kill.
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

    # Refuse superseded runs before touching Vercel. main is fast-forward
    # only, so a run whose sha is no longer the branch tip means a newer
    # promotion exists: going further would either report green for a commit
    # production no longer serves, or — via the forceNew retry below —
    # rebuild the old sha and Vercel would hand the production domain back
    # to it. Uses the checkout's persisted credentials.
    try:
        tip = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"Could not resolve the {branch} tip to rule out a superseded "
            f"run: {error.stderr.strip()}"
        ) from error
    if not tip or tip[0] != commit_sha:
        raise SystemExit(
            f"{branch} has moved to {tip[0] if tip else '<unknown>'}; this "
            f"run's {commit_sha} is superseded and must not touch production. "
            "The newer push's own Production Deploy run ships it."
        )

    project = api("GET", f"/v9/projects/{os.environ['VERCEL_PROJECT_ID']}")
    link = project.get("link") or {}
    if link.get("type") != "github" or not link.get("repoId"):
        raise SystemExit(
            "The Vercel project is not linked to a GitHub repository, so a "
            "production deployment cannot be created from a commit."
        )

    def create(force_new):
        return api(
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
            query={"forceNew": "1"} if force_new else None,
        )

    def unusable(deployment):
        """A deduplicated deployment this run must not adopt.

        Covers every state the polls below would reject, so a retry never
        replays a foregone failure: a non-production deployment (every
        promoted sha was once the staging tip, so a preview build of it
        usually exists, and a preview must not stand in for the production
        deploy), a build that already failed, a broken alias, and READY
        without the alias attached — a deployment this run just created is
        never READY yet, so that combination always means a stale prior
        build whose alias never landed.
        """
        return (
            deployment.get("target") != "production"
            or deployment.get("readyState") in FAILURE_STATES
            or bool(deployment.get("aliasError"))
            or (
                deployment.get("readyState") == "READY"
                and not deployment.get("aliasAssigned")
            )
        )

    # Without forceNew, Vercel deduplicates on the deployed sha and may hand
    # back an existing deployment instead of building. Usually that is right:
    # re-invoking for a sha whose production build exists returns it instead
    # of building twice. An unusable dedup result gets one forced fresh build.
    deployment = create(force_new=False)
    if unusable(deployment):
        print(
            "Deduplicated deployment is unusable "
            f"(target={deployment.get('target')!r}, "
            f"readyState={deployment.get('readyState')!r}, "
            f"aliasAssigned={deployment.get('aliasAssigned')!r}, "
            f"aliasError={deployment.get('aliasError')!r}); "
            "forcing a fresh production build"
        )
        deployment = create(force_new=True)
        if deployment.get("target") != "production":
            raise SystemExit(
                f"Vercel returned a non-production deployment "
                f"(target={deployment.get('target')!r}) for {commit_sha} even "
                "with forceNew; refusing to treat it as the production deploy."
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

    # aliasAssigned is historical: it stays truthy on a deployment after a
    # newer one takes the domain, so it proves assignment happened, not that
    # this deployment owns production NOW. Confirm current ownership from the
    # project's production target. Only a positively confirmed different
    # owner fails; an absent field keeps the poll verdict (older API shapes).
    # A few retries absorb propagation lag right after the alias flip.
    owner = None
    for _ in range(6):
        target = (
            api("GET", f"/v9/projects/{os.environ['VERCEL_PROJECT_ID']}").get(
                "targets"
            )
            or {}
        ).get("production") or {}
        owner = target.get("id") or target.get("uid")
        if owner in (None, deployment_id):
            break
        time.sleep(POLL_INTERVAL_S)
    if owner not in (None, deployment_id):
        raise SystemExit(
            f"Production is owned by deployment {owner}, not {deployment_id}; "
            "a newer deploy took the domain while this run was waiting."
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
