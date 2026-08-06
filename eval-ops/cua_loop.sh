#!/bin/bash
# Keep the CUA pipeline topped up to the cap without waiting for the 30-min
# babysit cron. CUA trials finish in ~15-30 min, so a half-hour refill interval
# leaves the pipeline running well under the cap for most of each cycle.
#
# A pass routinely takes ~30 min: submissions retry through HTTP 500s with
# backoff. Give each pass room to finish rather than truncating it mid-submit.
#
# cua_fill.py is cap-aware, config-driven and lock-protected, so this can
# safely overlap with the cron's own invocation -- whichever gets the lock does
# the work, and a phase flip is picked up without relaunching this loop.
set -u
cd /home/user/terra-run
for i in $(seq 1 200); do
  out=$(timeout 5400 python3 -u cua_fill.py 2>&1)
  echo "$(date -u +%FT%TZ) pass $i"
  echo "$out" | grep -E 'CUA fill:|submitted [0-9]+ this pass|at cap|all 12 cells|ABORT|skipping'
  if echo "$out" | grep -q 'all 12 cells hold their target'; then
    echo "CUA FILL COMPLETE $(date -u +%FT%TZ)"
    break
  fi
  sleep 120
done
