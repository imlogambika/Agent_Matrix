#!/usr/bin/env bash
# Fetch the public "Ghost Data Still Exists" dataset (HackerRank Orchestrate, October 2026)
# into ./dataset. It is synthetic, but it is the organisers' data, so it is not committed here.
#
#   bash scripts/fetch_dataset.sh            # -> ./dataset
#   bash scripts/fetch_dataset.sh /some/dir  # -> /some/dir
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="${1:-dataset}"
if [ -e "$DEST/deletion_requests.csv" ]; then
  echo "dataset already present at $DEST"
  exit 0
fi
WORK="$(mktemp -d)"
git clone --quiet --depth 1 --filter=blob:none --sparse \
  https://github.com/interviewstreet/hackerrank-orchestrate-october26.git "$WORK/upstream"
git -C "$WORK/upstream" sparse-checkout set dataset
mkdir -p "$(dirname "$DEST")"
mv "$WORK/upstream/dataset" "$DEST"
echo "dataset fetched into $DEST ($(find "$DEST" -type f | wc -l) files)"
