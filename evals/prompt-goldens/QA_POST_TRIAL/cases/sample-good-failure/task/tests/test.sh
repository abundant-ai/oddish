#!/bin/bash
# Runs the verifier and always writes reward.txt.
cd /app && python -m pytest /tests/test_calc.py -v
if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
