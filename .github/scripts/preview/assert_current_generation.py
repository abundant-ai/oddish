"""Fence stale preview generations.

GitHub's cancellation of a superseded workflow run is advisory: in-flight
steps may continue for minutes. This script is the ownership check every
mutating preview step runs first -- it compares the run's expected head SHA
against the PR branch's *current* tip, so a superseded run cannot deploy,
alias, publish, or comment over state owned by a newer generation.

Contract:
  - prints `current=true` (the run still owns the preview) or `current=false`
    (a newer push owns it) to stdout, and appends the same line to
    $GITHUB_OUTPUT when set
  - exits 0 in both cases: staleness is a neutral supersession, not a failure;
    callers skip their mutating work when current=false
  - exits 1 when the API cannot answer after retries: ownership is unproven,
    so failing closed is the safe error direction

Inputs (env vars):
  GH_TOKEN    - read access for the `gh` CLI
  OWNER_REPO  - e.g. "abundant-ai/oddish"
  HEAD_REF    - PR head branch name (no refs/heads/ prefix)
  HEAD_SHA    - the head SHA this run reconciles toward
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse

ATTEMPTS = 3
RETRY_DELAY_S = 5


def branch_tip_sha(owner_repo, head_ref):
    branch = urllib.parse.quote(head_ref, safe="")
    result = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            f"/repos/{owner_repo}/branches/{branch}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["commit"]["sha"]


def main():
    owner_repo = os.environ["OWNER_REPO"]
    head_ref = os.environ["HEAD_REF"]
    head_sha = os.environ["HEAD_SHA"]

    last_error = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            tip = branch_tip_sha(owner_repo, head_ref)
            break
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc
            if attempt < ATTEMPTS:
                time.sleep(RETRY_DELAY_S)
    else:
        print(
            f"::error::could not verify preview generation after {ATTEMPTS} "
            f"attempts: {last_error}",
            file=sys.stderr,
        )
        sys.exit(1)

    current = "true" if tip == head_sha else "false"
    if current == "false":
        print(
            f"::notice::preview generation superseded: branch {head_ref!r} is at "
            f"{tip[:12]}, this run reconciles {head_sha[:12]}; skipping mutations",
            file=sys.stderr,
        )
    print(f"current={current}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as f:
            f.write(f"current={current}\n")


if __name__ == "__main__":
    main()
