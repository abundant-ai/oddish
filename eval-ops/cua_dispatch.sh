#!/bin/bash
# Queue ALL remaining CUA trials straight to target, no wave-by-wave throttle.
#
# Charles, 2026-08-06: "the concurrency can go up to 25. just queue up all of
# the trials for the cua. trust me." The earlier <=10 wave filler existed
# because runbook 3.6 warns the browser grader hard-fails a trial (no reward
# file -> the trial errors) rather than queueing it when overloaded. That call
# is his; Oddish's own queue is the throttle from here.
#
# Target-based as always: --n-trials 10 creates 10 - existing per cell.
set -u
HERE=/home/user/terra-run
ODDISH=/home/user/oddish/oddish/.venv/bin/oddish
cd $HERE || exit 1
set -a; source /home/user/oddish/.env; set +a
LOG=$HERE/cua_dispatch.log
N=$(python3 -c "import json;print(json.load(open('config.json'))['cua_target'])")

TASKS="excel-clone mastodon-clone s3-clone slack-clone"
echo "=== CUA dispatch to N=$N start $(date -u +%FT%TZ) ===" >>$LOG

for pair in "LOW 17b6f7d9 low" "MEDIUM c071229f medium" "HIGH a706e700 high"; do
  set -- $pair
  ARM=$1 EXP=$2 EFFORT=$3
  for T in $TASKS; do
    ok=0
    for attempt in 1 2 3 4 5 6; do
      out=$(cd $HERE/ds-${ARM,,} && timeout 400 $ODDISH run -p . -t "$T" \
            -a codex -m openai/gpt-5.6-terra --n-trials $N -e modal -E "$EXP" \
            --override-memory-mb 65536 --no-baseline-gate \
            --ak "reasoning_effort=$EFFORT" \
            --ae "ODDISH_EVAL_NONCE=$ARM-$T-$(date +%s%N)" \
            --background --force 2>&1)
      if echo "$out" | grep -qi 'new version'; then
        echo "$(date -u +%T) $ARM $T -> ABORT: wants NEW TASK VERSION" >>$LOG
        break
      fi
      if echo "$out" | grep -q 'Task submitted!'; then
        tr=$(echo "$out" | grep -Eio 'trials: *[0-9]+' | head -1)
        echo "$(date -u +%T) $ARM $T attempt$attempt -> submitted ($tr)" >>$LOG
        ok=1; break
      fi
      err=$(echo "$out" | grep -Eo 'HTTP [0-9]+|Internal Server Error' | head -1)
      echo "$(date -u +%T) $ARM $T attempt$attempt -> FAIL ${err:-TIMEOUT/NO-OUTPUT}" >>$LOG
      sleep $((5 * attempt * attempt))
    done
    [ $ok -eq 1 ] || echo "$(date -u +%T) $ARM $T -> GAVE UP" >>$LOG
  done
done
echo "=== CUA dispatch done $(date -u +%FT%TZ) ===" >>$LOG
