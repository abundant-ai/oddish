#!/bin/bash
# Re-run the opencode/gemini eval at 10 trials/task now that the credential
# mirror is deployed (verified: ~380k input tokens vs 0 before).
# Serial, one task per command, with backoff -- concurrent submits 500.
set -u
HERE=/home/user/terra-run
ODDISH=/home/user/oddish/oddish/.venv/bin/oddish
set -a; source /home/user/oddish/.env; set +a
LOG=$HERE/opencode-dispatch.log
cd $HERE/ds-none || exit 1
TASKS="biofabric-rust-rewrite embedding-eval excel-clone find-network-alignments \
jax-pytorch-rewrite kubernetes-rust-rewrite mastodon-clone nextjs-vite-rewrite \
parameter-golf post-train-ifeval-gpu ruby-rust-port rust-c-compiler rust-java-lsp \
s3-clone slack-clone stripe-clone trimul-cuda vliw-kernel-optimization wasm-simd \
zstd-decoder"
echo "=== opencode rerun to 10 start $(date -u +%FT%TZ) ===" >>$LOG
for T in $TASKS; do
  for attempt in 1 2 3 4 5; do
    out=$(timeout 900 $ODDISH run -p . -t "$T" -a opencode -m google/gemini-3.7-flash \
          --n-trials 10 -e modal -E 826d7d88 --force --background 2>&1)
    if echo "$out" | grep -qi 'new version'; then
      echo "$(date -u +%T) $T -> ABORT: NEW TASK VERSION" >>$LOG; break; fi
    if echo "$out" | grep -qi 'budget'; then
      echo "$(date -u +%T) $T -> BUDGET LIMIT" >>$LOG; break; fi
    if echo "$out" | grep -q 'Task submitted!'; then
      echo "$(date -u +%T) $T attempt$attempt -> $(echo "$out" | grep -Eio 'trials: *[0-9]+' | head -1)" >>$LOG
      break; fi
    echo "$(date -u +%T) $T attempt$attempt -> FAIL" >>$LOG
    sleep $((10 * attempt))
  done
done
echo "=== opencode rerun done $(date -u +%FT%TZ) ===" >>$LOG
