#!/usr/bin/env bash
# publish_public_repo.sh — sync the CURRENT local tree into the public mirror
# repo as ONE squashed-style commit, without ever touching local history.
#
# How it works:
#   1. clone the PUBLIC repo into a temp dir (its history is release-only)
#   2. overlay the local repo's tracked files (git archive of HEAD)
#   3. commit + push to the public repo
#
# Usage:
#   ORG=gcap0n1 REPO=neural-tape bash tools/publish_public_repo.sh "release message"
set -euo pipefail

ORG="${ORG:-gcap0n1}"
REPO="${REPO:-neural-tape}"
MSG="${1:-sync release}"

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[publish] cloning public repo $ORG/$REPO -> $TMP"
git clone -q "https://github.com/$ORG/$REPO.git" "$TMP/repo"

echo "[publish] overlaying tracked tree from local HEAD"
cd "$ROOT"
git archive HEAD | tar -x -C "$TMP/repo"

cd "$TMP/repo"
git add -A
if git diff --cached --quiet; then
    echo "[publish] no changes vs public repo — nothing to push"
    exit 0
fi
git commit -q -m "$MSG"
echo "[publish] pushing"
git push -q origin main
echo "[publish] done: https://github.com/$ORG/$REPO"