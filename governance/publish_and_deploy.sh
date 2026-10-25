#!/usr/bin/env bash
# Publish the Agent Matrix policies to your FailproofAI Cloud org and deploy them to one machine.
#
# Prerequisites (a person has to do these):
#   1. this machine is connected:   failproofai config      (wizard: installs the daemon, needs sudo)
#   2. you are signed in to fp:     fp login                (emails a one-time code)
#
#   bash governance/publish_and_deploy.sh <machine-id> [observe|enforce]
#
# The machine id is in `fp --json fleet list`. Start with `observe` (default): the policies are
# evaluated and reported but block nothing. Re-run with `enforce` once guardrails look right:
#   fp guardrails summary --since 24h
set -euo pipefail
cd "$(dirname "$0")/policies"
MACHINE="${1:?usage: publish_and_deploy.sh <machine-id> [observe|enforce]}"
EFFECT="${2:-observe}"
case "$EFFECT" in observe|enforce) ;; *) echo "effect must be observe or enforce" >&2; exit 2 ;; esac

fp --json whoami | grep -q '"logged_in": true' || { echo "not signed in: run fp login" >&2; exit 4; }

declare -A DESCRIPTION=(
  [protect-audit-evidence]="Evidence under dataset/ is read-only for agents"
  [block-verdict-tampering]="output.csv and the review queue come only from the guarded pipeline"
  [block-fabricated-model-replies]="The model reply cache is never hand-edited"
  [block-evidence-exfiltration]="Audit evidence never leaves the machine"
  [block-private-data-commit]="Dataset, weights and credentials are never committed"
  [warn-decision-logic-change]="Oversight before changing the code that decides COMPLETE"
)
refs=()
for name in "${!DESCRIPTION[@]}"; do
  id="agent-matrix-$name"
  fp policies publish "$id" "$name-policies.mjs" --description "${DESCRIPTION[$name]}"
  refs+=(--add "$id:$EFFECT")
done
# --add amends the machine's current set; nothing already deployed is removed
fp fleet deploy "$MACHINE" "${refs[@]}"
fp fleet show "$MACHINE"
