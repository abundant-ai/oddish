#!/bin/bash
# Keep the opencode/gemini arm filling, evenly, in small batches, until every
# task holds 10 valid trials.
#
# Two throttles, both deliberate:
#   --batch 3  queue three at a time, spread fewest-held-first, so all 20 tasks
#              advance together instead of one task draining the whole quota.
#   --cap 20   global in-flight ceiling. Measured, not guessed: at 34-35
#              concurrent every single completion came back 429 RESOURCE_EXHAUSTED
#              (Gemini allows 20M input tokens/minute, shared fleet-wide), while
#              at ~15 concurrent completions come back clean with real rewards.
#
# --ak variant=high is LOAD-BEARING. opencode's `variant` is a CliFlag with no
# default, so dropping it silently runs a different configuration than the arm
# we are trying to reproduce.
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

  out=$(timeout 1500 python3 -u even_fill.py 826d7d88 opencode \
        google/gemini-3.7-flash --target 10 --cap 20 --batch 3 \
        --ak variant=high 2>&1)
  echo "$out" | sed "s/^/[pass $pass] /" >>$LOG

  if echo "$out" | grep -q 'COMPLETE'; then
    echo "$(date -u +%FT%TZ) OPENCODE FILL COMPLETE — 20 tasks x 10 valid" >>$LOG
    break
  fi
  sleep 60
done
