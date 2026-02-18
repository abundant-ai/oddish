#!/bin/bash

cd /app/src

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "tests/lib"
cp "/tests/lib/api.js" "tests/lib/api.js"
mkdir -p "tests/lib/eslint"
cp "/tests/lib/eslint/eslint.js" "tests/lib/eslint/eslint.js"
mkdir -p "tests/lib/eslint"
cp "/tests/lib/eslint/legacy-eslint.js" "tests/lib/eslint/legacy-eslint.js"

# Run the specific test files using mocha
npx mocha tests/lib/api.js tests/lib/eslint/eslint.js tests/lib/eslint/legacy-eslint.js
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
