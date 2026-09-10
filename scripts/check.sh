#!/usr/bin/env bash
# Everything that must pass before a commit or a tag. Offline, no credentials.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 scripts/validate.py
echo
for suite in scripts/test_*.py; do
  echo "--- $suite"
  python3 "$suite"
done
