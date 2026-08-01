#!/bin/bash
cat > /app/report.py <<'EOF'
from collections import Counter

with open("input.txt") as f:
    counts = Counter(f.read().split())

for word, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
    print(f"{word} {count}")
EOF
