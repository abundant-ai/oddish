#!/bin/bash

cd /app/src

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "tests/fixtures/emfile"
cp "/tests/fixtures/emfile/eslint.config.js" "tests/fixtures/emfile/eslint.config.js"

# Also copy the EMFILE handling test script
mkdir -p "tools"
cp "/tests/tools/check-emfile-handling.js" "tools/check-emfile-handling.js"

# Set a low file descriptor limit to force EMFILE errors
# This makes the test actually trigger the EMFILE condition that needs retry handling
ulimit -n 256

# Run the EMFILE handling test
npm run test:emfile
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
