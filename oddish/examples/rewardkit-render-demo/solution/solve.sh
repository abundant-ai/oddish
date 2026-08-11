#!/usr/bin/env bash
# Deliberately imperfect oracle: passes the `files` dimension, partially
# satisfies `style` (short report), and fails `completeness` (header-only
# CSV) so one trial shows pass, partial, and fail dimensions at once.
set -euo pipefail
cd /app
cat > report.md <<'MD'
# RewardKit render demo

This report exists so the verifier has something to score. It has the marker
line above and a short body of roughly thirty-five words in total, which is
deliberately fewer than the sixty words the style check wants.
MD
printf 'line one\nline two\nline three\n' > summary.txt
mkdir -p data
printf 'metric,value\n' > data/metrics.csv
