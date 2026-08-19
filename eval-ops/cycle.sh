#!/bin/bash
# ONE self-healing pass of the phase-B fill. Safe to run from a cold container.
#
# Design note: this container is being recycled roughly every 45 minutes, which
# wipes /home/user/terra-run and kills any long-running loop. So the unit of
# work is a single idempotent pass that (a) rebuilds itself from git if needed
# and (b) re-asserts targets. Sweeps are target-based, so a pass that runs
# twice, or runs after a crash halfway through, costs nothing extra.
#
# Ordering is deliberate: canary first (probe the WRITE path -- during the
# 02:00Z degradation reads answered fine while every submit 500'd), then the
# non-CUA arms serially, then one throttled CUA pass.
set -u
HERE=/home/user/terra-run
ODDISH=/home/user/oddish/oddish/.venv/bin/oddish
LOG=$HERE/cycle.log

# --- self-heal: restore from the git backup if the container was recycled
if [ ! -f $HERE/dispatch.sh ]; then
  mkdir -p $HERE
  cd /home/user/oddish || exit 1
  git fetch origin claude/oddish-api-env-setup-f76jpp >/dev/null 2>&1
  git merge --ff-only origin/claude/oddish-api-env-setup-f76jpp >/dev/null 2>&1
  cp /home/user/oddish/eval-ops/* $HERE/ 2>/dev/null
  echo "$(date -u +%FT%TZ) RECOVERED toolkit from git" >>$LOG
fi
for a in low medium high; do
  [ -d $HERE/ds-$a ] || cp -r /home/user/swe-marathon/tasks $HERE/ds-$a
done

cd $HERE || exit 1
set -a; source /home/user/oddish/.env; set +a
N=$(python3 -c "import json;print(json.load(open('config.json'))['noncua_target'])")

# --- canary: re-assert a cell already at target. Creates ZERO trials, so it is
# a free probe of upload+submit -- the path that actually has to work.
out=$(cd ds-low && timeout 300 $ODDISH run -p . -t biofabric-rust-rewrite \
      -a codex -m openai/gpt-5.6-terra --n-trials $N -e modal -E 17b6f7d9 \
      --override-memory-mb 65536 --no-baseline-gate --ak reasoning_effort=low \
      --ae "ODDISH_EVAL_NONCE=canary-$(date +%s%N)" --background --force 2>&1)
if ! echo "$out" | grep -q 'Task submitted!'; then
  echo "$(date -u +%FT%TZ) write path DOWN — skipping this pass" >>$LOG
  exit 0
fi
echo "$(date -u +%FT%TZ) write path UP — dispatching (target $N)" >>$LOG

rm -f dispatch-LOW.log dispatch-MEDIUM.log dispatch-HIGH.log
bash dispatch.sh >/dev/null 2>&1
created=$(grep -h submitted dispatch-*.log 2>/dev/null \
          | grep -oE 'Trials: *[0-9]+' | awk '{s+=$2} END {print s+0}')
ok=$(grep -h submitted dispatch-*.log 2>/dev/null | wc -l)
echo "$(date -u +%FT%TZ) non-CUA: ok=$ok/48 created=$created" >>$LOG

timeout 3000 python3 -u cua_fill.py >>cua_loop.log 2>&1
tail -1 cua_loop.log >>$LOG

echo "$(date -u +%FT%TZ) pass done" >>$LOG
