#!/bin/bash
# Long-running driver for phase B. Runs until every cell holds its target.
#
# Long loops are viable again: the container was being reclaimed for INACTIVITY
# (three wipes), and a 20-minute session heartbeat now keeps it warm. The
# self-heal step stays anyway -- it is cheap and makes a cold start harmless.
#
# Each cycle: canary the write path, dispatch non-CUA, re-assert CUA. All
# target-based, so any cycle is a no-op where the work is already done.
set -u
HERE=/home/user/terra-run
ODDISH=/home/user/oddish/oddish/.venv/bin/oddish
LOG=$HERE/forever.log

for cycle in $(seq 1 500); do
  if [ ! -f $HERE/dispatch.sh ]; then
    cd /home/user/oddish || exit 1
    git fetch origin claude/oddish-api-env-setup-f76jpp >/dev/null 2>&1
    git merge --ff-only origin/claude/oddish-api-env-setup-f76jpp >/dev/null 2>&1
    mkdir -p $HERE && cp /home/user/oddish/eval-ops/* $HERE/ 2>/dev/null
    echo "$(date -u +%FT%TZ) RECOVERED from git" >>$LOG
  fi
  for a in low medium high; do
    [ -d $HERE/ds-$a ] || cp -r /home/user/swe-marathon/tasks $HERE/ds-$a
  done

  cd $HERE || exit 1
  set -a; source /home/user/oddish/.env; set +a
  N=$(python3 -c "import json;print(json.load(open('config.json'))['noncua_target'])")

  # Canary: re-asserting a cell already at target creates ZERO trials, so this
  # probes upload+submit for free. Reads can answer fine while writes 500.
  out=$(cd ds-low && timeout 300 $ODDISH run -p . -t biofabric-rust-rewrite \
        -a codex -m openai/gpt-5.6-terra --n-trials $N -e modal -E 17b6f7d9 \
        --override-memory-mb 65536 --no-baseline-gate --ak reasoning_effort=low \
        --ae "ODDISH_EVAL_NONCE=canary-$(date +%s%N)" --background --force 2>&1)
  if ! echo "$out" | grep -q 'Task submitted!'; then
    echo "$(date -u +%FT%TZ) cycle $cycle: write path DOWN — backing off" >>$LOG
    sleep 600
    continue
  fi

  rm -f dispatch-LOW.log dispatch-MEDIUM.log dispatch-HIGH.log
  bash dispatch.sh >/dev/null 2>&1
  nc_created=$(grep -h submitted dispatch-*.log 2>/dev/null \
               | grep -oE 'Trials: *[0-9]+' | awk '{s+=$2} END {print s+0}')
  nc_ok=$(grep -h submitted dispatch-*.log 2>/dev/null | wc -l)

  : > cua_dispatch.log
  bash cua_dispatch.sh >/dev/null 2>&1
  cua_created=$(grep -h submitted cua_dispatch.log 2>/dev/null \
                | grep -oE 'Trials: *[0-9]+' | awk '{s+=$2} END {print s+0}')
  cua_ok=$(grep -h submitted cua_dispatch.log 2>/dev/null | wc -l)

  echo "$(date -u +%FT%TZ) cycle $cycle: nonCUA ok=$nc_ok/48 created=$nc_created | CUA ok=$cua_ok/12 created=$cua_created" >>$LOG

  if [ "$nc_ok" -ge 48 ] && [ "$nc_created" -eq 0 ] \
     && [ "$cua_ok" -ge 12 ] && [ "$cua_created" -eq 0 ]; then
    echo "$(date -u +%FT%TZ) PHASE B COMPLETE — all 60 cells at target" >>$LOG
    break
  fi
  sleep 600
done
