#!/bin/bash

cd /app/src

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "tests/fixtures/cjs-config"
cp "/tests/fixtures/cjs-config/eslint.config.cjs" "tests/fixtures/cjs-config/eslint.config.cjs"
mkdir -p "tests/fixtures/cjs-config"
cp "/tests/fixtures/cjs-config/foo.js" "tests/fixtures/cjs-config/foo.js"
mkdir -p "tests/fixtures/js-mjs-cjs-config"
cp "/tests/fixtures/js-mjs-cjs-config/eslint.config.cjs" "tests/fixtures/js-mjs-cjs-config/eslint.config.cjs"
mkdir -p "tests/fixtures/js-mjs-cjs-config"
cp "/tests/fixtures/js-mjs-cjs-config/eslint.config.js" "tests/fixtures/js-mjs-cjs-config/eslint.config.js"
mkdir -p "tests/fixtures/js-mjs-cjs-config"
cp "/tests/fixtures/js-mjs-cjs-config/eslint.config.mjs" "tests/fixtures/js-mjs-cjs-config/eslint.config.mjs"
mkdir -p "tests/fixtures/js-mjs-cjs-config"
cp "/tests/fixtures/js-mjs-cjs-config/foo.js" "tests/fixtures/js-mjs-cjs-config/foo.js"
mkdir -p "tests/fixtures/mjs-cjs-config"
cp "/tests/fixtures/mjs-cjs-config/eslint.config.cjs" "tests/fixtures/mjs-cjs-config/eslint.config.cjs"
mkdir -p "tests/fixtures/mjs-cjs-config"
cp "/tests/fixtures/mjs-cjs-config/eslint.config.mjs" "tests/fixtures/mjs-cjs-config/eslint.config.mjs"
mkdir -p "tests/fixtures/mjs-cjs-config"
cp "/tests/fixtures/mjs-cjs-config/foo.js" "tests/fixtures/mjs-cjs-config/foo.js"
mkdir -p "tests/fixtures/mjs-config"
cp "/tests/fixtures/mjs-config/eslint.config.mjs" "tests/fixtures/mjs-config/eslint.config.mjs"
mkdir -p "tests/fixtures/mjs-config"
cp "/tests/fixtures/mjs-config/foo.js" "tests/fixtures/mjs-config/foo.js"
mkdir -p "tests/lib/eslint"
cp "/tests/lib/eslint/flat-eslint.js" "tests/lib/eslint/flat-eslint.js"

# Run the specific test files using mocha
npx mocha tests/lib/eslint/flat-eslint.js
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
