#!/usr/bin/env bash
# run-private-backup.sh — the weekly leg that gets the encrypted backup OFF this laptop.
#
# bin/backup-private.sh only writes the blob and prints the two git commands to run. By
# hand that is fine, because a human reads the output. On a schedule nobody reads it, and
# a backup that is written and never pushed is a backup of this laptop onto this laptop —
# exactly the failure it exists to prevent. So this runs the encrypt and then commits and
# pushes it, and says so loudly when the push is the part that failed.
#
# Only private-backup/ is committed, by explicit path. A scheduled job must never sweep up
# whatever the person happened to have in the tree at 20:00 on a Sunday.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || exit 1

echo "=== private backup $(date '+%Y-%m-%d %H:%M:%S %Z') ==="

# Committing in the middle of a merge or rebase would fold the backup into whatever is
# half-finished. Wait for next week instead; the roster does not change hourly.
GITDIR="$(git rev-parse --git-dir 2>/dev/null)" || { echo "not a git repo"; exit 1; }
for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
  if [ -e "$GITDIR/$marker" ]; then
    echo "skipped: a $marker is in progress, so this is not the moment to commit"
    exit 0
  fi
done

if ! bin/backup-private.sh; then
  echo "!! the encrypt step failed — nothing was committed"
  exit 1
fi

# Nothing to do when the ignored files have not changed: an identical blob would still be
# a NEW blob every week, because gpg salts each run, so the question has to be asked of
# the manifest and the tracked state, not of the bytes.
if git diff --quiet -- private-backup/ \
   && [ -z "$(git ls-files -o --exclude-standard private-backup/)" ]; then
  echo "no change since the last backup — nothing to commit"
  exit 0
fi

git add private-backup/ || exit 1
if ! git commit -q -m "backup: refresh encrypted config ($(date -u '+%Y-%m-%d'))" \
       -- private-backup/; then
  echo "!! commit refused (a hook, most likely) — the backup is only on this laptop"
  exit 1
fi
echo "    committed $(git rev-parse --short HEAD)"

# The push runs the full pre-push gate, suite included. That is slow and it is the point:
# this pushes the branch, not just the blob, and a scheduled job is the last place to
# start waving gates through.
if ! git push private HEAD; then
  echo "!! PUSH TO private FAILED — the backup is committed but still only on this laptop."
  echo "   Run: git push private HEAD   (and read why the gate refused)"
  exit 1
fi
echo "    pushed to private. $(date '+%H:%M:%S')"
