#!/bin/bash
# Raise the terra arm to N trials per task in all three effort experiments.
# N comes from config.json (phase B = 10).
#
# Sweeps are TARGET-based (runbook 3.1): --n-trials N creates N - existing for
# that (task, agent, model, experiment). Re-running is therefore safe and never
# double-submits, which matters because the API intermittently times out or
# 500s under load without telling us whether the submit committed.
#
# The 4 CUA-verifier tasks are deliberately NOT here -- they go through
# cua_fill.py, which throttles to the runbook 3.6 concurrency cap.
set -u
set -a; source /home/user/oddish/.env; set +a

ODDISH=/home/user/oddish/oddish/.venv/bin/oddish
HERE=/home/user/terra-run
N=$(python3 -c "import json;print(json.load(open('$HERE/config.json'))['noncua_target'])")
TASKS="biofabric-rust-rewrite embedding-eval find-network-alignments \
jax-pytorch-rewrite kubernetes-rust-rewrite nextjs-vite-rewrite parameter-golf \
post-train-ifeval-gpu ruby-rust-port rust-c-compiler rust-java-lsp stripe-clone \
trimul-cuda vliw-kernel-optimization wasm-simd zstd-decoder"

arm_run() {   # $1=arm label  $2=exp id  $3=reasoning_effort
  local ARM=$1 EXP=$2 EFFORT=$3
  local LOG=$HERE/dispatch-${ARM}.log
  cd "$HERE/ds-${ARM,,}" || return 1
  echo "=== $ARM ($EXP, effort=$EFFORT) target=$N start $(date -u +%FT%TZ) ===" >>"$LOG"
  for T in $TASKS; do
    local nonce="${ARM}-${T}-$(date +%s%N)"
    local ok=0
    for attempt in 1 2 3 4 5; do
      out=$(timeout 400 $ODDISH run -p . -t "$T" -a codex -m openai/gpt-5.6-terra \
            --n-trials $N -e modal -E "$EXP" --override-memory-mb 65536 \
            --no-baseline-gate --ak "reasoning_effort=$EFFORT" \
            --ae "ODDISH_EVAL_NONCE=$nonce" --background --force 2>&1)
      if echo "$out" | grep -qi 'new version'; then
        echo "$(date -u +%T) $ARM $T -> ABORT: wants NEW TASK VERSION" >>"$LOG"
        break
      fi
      if echo "$out" | grep -q 'Task submitted!'; then
        trials=$(echo "$out" | grep -Eio 'trials: *[0-9]+' | head -1)
        echo "$(date -u +%T) $ARM $T attempt$attempt -> submitted ($trials)" >>"$LOG"
        ok=1; break
      fi
      err=$(echo "$out" | grep -Eo 'HTTP [0-9]+|Internal Server Error' | head -1)
      echo "$(date -u +%T) $ARM $T attempt$attempt -> FAIL ${err:-TIMEOUT/NO-OUTPUT}" >>"$LOG"
      sleep $((5 * attempt * attempt))
    done
    [ $ok -eq 1 ] || echo "$(date -u +%T) $ARM $T -> GAVE UP" >>"$LOG"
  done
  echo "=== $ARM done $(date -u +%FT%TZ) ===" >>"$LOG"
}

arm_run LOW    17b6f7d9 low    &
arm_run MEDIUM c071229f medium &
arm_run HIGH   a706e700 high   &
wait
echo "DISPATCH COMPLETE $(date -u +%FT%TZ)" >>$HERE/dispatch-ALL.log
