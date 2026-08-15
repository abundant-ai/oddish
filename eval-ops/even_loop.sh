#!/bin/bash
# Keep the opencode/gemini arm filling, evenly, until every task holds 10 valid
# trials.
#
#   --per-task-inflight 3    keep three trials in flight for EVERY task, so all
#                            of them advance together instead of one draining
#                            the queue. Submits go out neediest-first (fewest
#                            valid trials), which is what closes the gap between
#                            the nearly-done tasks and the ones near zero.
#   --min-total-inflight 40  Oddish runs 20 concurrently, so this keeps roughly
#                            20 more queued behind them and the fleet never goes
#                            idle. As tasks finish, 3-per-task alone stops being
#                            enough to fill that, and the surplus goes to the
#                            tasks with the fewest valid trials.
#
# Note on rate limits: Gemini allows 20M input tokens/minute shared fleet-wide.
# An earlier run at 34-35 concurrent had every completion come back
# 429 RESOURCE_EXHAUSTED. Watch the valid count -- if it stops climbing while
# trials keep finishing, the fleet is back over the ceiling.
#
# Submits go through oc_sweep.yaml rather than --ak. That file carries BOTH
# variant=high (a CliFlag with no default -- dropping it silently changes the
# configuration) and opencode_config.compaction.reserved, which is nested and
# therefore cannot travel through --ak at all.
#
# Self-heals from git if the container is recycled mid-run.
set -u
HERE=/home/user/terra-run
LOG=$HERE/even_loop.log

for pass in $(seq 1 4000); do
  if [ ! -f $HERE/even_fill.py ]; then
    cd /home/user/oddish || exit 1
    git fetch origin claude/oddish-api-env-setup-f76jpp >/dev/null 2>&1
    git merge --ff-only origin/claude/oddish-api-env-setup-f76jpp >/dev/null 2>&1
    mkdir -p $HERE && cp /home/user/oddish/eval-ops/* $HERE/ 2>/dev/null
    echo "$(date -u +%FT%TZ) RECOVERED toolkit from git" >>$LOG
  fi
  [ -d $HERE/ds-none ] || cp -r /home/user/swe-marathon/tasks $HERE/ds-none

  cd $HERE || exit 1
  set -a; source /home/user/oddish/.env; set +a

  # A per-task pass polls 20 tasks and then issues up to 20 submits, each of
  # which uploads the dataset -- far longer than the round-robin pass this
  # timeout was sized for. A kill mid-pass is harmless (target-based, the next
  # pass resumes) but wastes the poll, so give it room to finish.
  out=$(timeout 3000 python3 -u even_fill.py 826d7d88 opencode \
        google/gemini-3.7-flash --target 10 --cap 9999 --per-task-inflight 3 \
        --min-total-inflight 40 --sweep-template $HERE/oc_sweep.yaml 2>&1)
  echo "$out" | sed "s/^/[pass $pass] /" >>$LOG

  if echo "$out" | grep -q 'COMPLETE'; then
    echo "$(date -u +%FT%TZ) OPENCODE FILL COMPLETE — 20 tasks x 10 valid" >>$LOG
    break
  fi
  sleep 60
done
