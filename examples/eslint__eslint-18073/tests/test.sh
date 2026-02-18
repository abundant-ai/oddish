#!/bin/bash

cd /app/src

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "tests/fixtures/testers/rule-tester"
cp "/tests/fixtures/testers/rule-tester/messageId.js" "tests/fixtures/testers/rule-tester/messageId.js"
mkdir -p "tests/fixtures/testers/rule-tester"
cp "/tests/fixtures/testers/rule-tester/suggestions.js" "tests/fixtures/testers/rule-tester/suggestions.js"
mkdir -p "tests/lib/linter"
cp "/tests/lib/linter/interpolate.js" "tests/lib/linter/interpolate.js"
mkdir -p "tests/lib/rule-tester"
cp "/tests/lib/rule-tester/rule-tester.js" "tests/lib/rule-tester/rule-tester.js"

# Run the specific test files using mocha
npx mocha tests/lib/linter/interpolate.js tests/lib/rule-tester/rule-tester.js
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
