#!/usr/bin/env bash
# Prove every Agent Matrix policy decides correctly before it is published.
# Each policy runs in an isolated sandbox (throwaway project + HOME) through Failproof's own hook
# runner, using FailproofAI's published test script; nothing on this machine is changed.
#
#   bash governance/test_policies.sh
set -euo pipefail
cd "$(dirname "$0")/policies"
TESTER="${TMPDIR:-/tmp}/agent-matrix-policy-tester/test-policy.mjs"
if [ ! -f "$TESTER" ]; then
  mkdir -p "$(dirname "$TESTER")"
  curl -fsSL -o "$TESTER" \
    https://raw.githubusercontent.com/FailproofAI/skills/main/skills/failproofai-policy-author/scripts/test-policy.mjs
fi
status=0
for cases in tests/*.json; do
  name="$(basename "$cases" .json)"
  echo "== $name"
  node "$TESTER" --policy "$name-policies.mjs" --cases "$cases" || status=1
done
exit $status
