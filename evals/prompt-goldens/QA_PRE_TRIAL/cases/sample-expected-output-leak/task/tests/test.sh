#!/bin/bash
cd /app && python report.py > report_out.txt
diff -u /app/expected_output.txt /app/report_out.txt
if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
