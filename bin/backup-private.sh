#!/usr/bin/env bash
# backup-private.sh — an encrypted copy of the files git is told to ignore.
#
# The public mirror carries the code. The private repo carries the history. NEITHER
# carries the things that are actually hard to replace, because they are gitignored for
# good reasons: the roster of 395 people with their Jira and Slack ids, the org config,
# the hand-curated denylist, the publish script itself, the runbooks that name people.
# Lose the laptop and the code is fine and those are gone.
#
# So they are tarred, encrypted with a passphrase, and committed to the PRIVATE repo as
# one opaque blob. Encrypted because a private repo is still a repo: a wrong click on
# visibility, a stolen token or a future collaborator should not hand anyone the roster.
#
# NOT included: ~/.secrets. Those are API tokens, and a token is revoked and reissued in
# a minute. The roster is not. Backing up live credentials to a repo buys little and
# risks a lot.
#
#   bin/backup-private.sh            # encrypt and stage the blob
#   bin/backup-private.sh --restore  # decrypt it back over the working tree
#
# The passphrase lives in the macOS keychain so a scheduled run needs no typing. It is
# ALSO printed once, on first run, because a passphrase stored only on the machine being
# backed up protects nothing the day that machine dies. Put it in your password manager.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

BLOB="private-backup/ignored-files.tar.gz.gpg"
MANIFEST="private-backup/MANIFEST.txt"
KEYCHAIN_SERVICE="synapse-private-backup"

# What goes in. Globs, resolved at run time, so a new config file is picked up without
# editing this list. Only files git is ignoring are worth the trouble; anything tracked
# is already in the private repo.
INCLUDE=(
  "work-context/config/*.yaml"
  ".publish-denylist.txt"
  "bin/publish-synapse.sh"
  "bin/leak-patterns.public.txt"
  "runbook/*.md"
  ".claude/settings.local.json"
)

passphrase() {
  local pw
  pw="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null)"
  if [ -n "$pw" ]; then
    printf '%s' "$pw"
    return 0
  fi
  # First run: make one, keep it, and show it once.
  pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)"
  security add-generic-password -s "$KEYCHAIN_SERVICE" -a "$USER" -w "$pw" -U >/dev/null
  {
    echo ""
    echo "  A passphrase was generated and stored in your login keychain."
    echo "  WRITE IT DOWN NOW, in your password manager:"
    echo ""
    echo "      $pw"
    echo ""
    echo "  It is not in the repo and not recoverable from the backup. A passphrase that"
    echo "  exists only on this laptop is worth nothing the day this laptop dies."
    echo ""
  } >&2
  printf '%s' "$pw"
}

# The passphrase goes in a file with no group or other permission, used, and removed.
# Process substitution looked neater and silently produced a 75-byte blob that still
# "verified", which is the worst way for a backup to fail.
pw_file() {
  local f
  f="$(mktemp -t synapse-backup-pw)"
  chmod 600 "$f"
  passphrase > "$f"
  printf '%s' "$f"
}

if [ "${1:-}" = "--restore" ]; then
  [ -f "$BLOB" ] || { echo "no backup at $BLOB" >&2; exit 1; }
  echo "==> Restoring $BLOB over the working tree"
  PW="$(pw_file)"
  trap 'rm -f "$PW"' EXIT
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PW" \
      --decrypt "$BLOB" | tar -xzf - -C "$REPO" \
    || { echo "restore FAILED" >&2; exit 1; }
  echo "    restored."
  exit 0
fi

# ---- collect ----------------------------------------------------------------
FILES=()
for pat in "${INCLUDE[@]}"; do
  for f in $pat; do
    [ -f "$f" ] || continue
    # Tracked files are already in the private repo; this is for what git ignores.
    git ls-files --error-unmatch "$f" >/dev/null 2>&1 && continue
    case "$f" in *.example.yaml) continue;; esac     # examples ship publicly already
    FILES+=("$f")
  done
done

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "nothing to back up — every match is already tracked" >&2
  exit 1
fi

mkdir -p "$(dirname "$BLOB")"
PW="$(pw_file)"
trap 'rm -f "$PW" "$TMP_TAR"' EXIT
TMP_TAR="$(mktemp -t synapse-backup-tar)"

# Tar to a FILE first. Streaming straight into gpg hid a failure: gpg exited, tar
# reported a write error, and an almost-empty blob was left behind looking fine.
if ! tar -czf "$TMP_TAR" "${FILES[@]}"; then
  echo "could not read every file — refusing to write a partial backup" >&2
  exit 1
fi

echo "==> Encrypting ${#FILES[@]} file(s) ($(wc -c <"$TMP_TAR" | tr -d ' ') bytes)"
if ! gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PW" \
        --symmetric --cipher-algo AES256 --output "$BLOB" "$TMP_TAR"; then
  echo "encryption failed" >&2
  rm -f "$BLOB"
  exit 1
fi

# Prove the round trip NOW, by file LIST and not just by tar exiting 0: an empty archive
# also exits 0, which is how the first version called a 75-byte blob verified.
BACK="$(gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PW" \
            --decrypt "$BLOB" 2>/dev/null | tar -tzf - 2>/dev/null | sort)"
WANT="$(printf '%s\n' "${FILES[@]}" | sort)"
if [ "$BACK" != "$WANT" ]; then
  echo "the backup does not decrypt back to the same file list — refusing it" >&2
  echo "   wanted: $(printf '%s\n' "${FILES[@]}" | wc -l | tr -d ' ') file(s)" >&2
  echo "   got:    $(printf '%s' "$BACK" | grep -c . || true) file(s)" >&2
  rm -f "$BLOB"
  exit 1
fi

# A readable manifest beside the blob: which files are inside and how big, so a future
# restore knows what it is getting without the passphrase. Names only, never contents.
{
  echo "# Encrypted backup of the files git ignores. Contents are in $(basename "$BLOB")."
  echo "# Written by bin/backup-private.sh. Restore: bin/backup-private.sh --restore"
  echo "# Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo ""
  for f in "${FILES[@]}"; do
    printf '%10s  %s\n' "$(wc -c <"$f" | tr -d ' ')" "$f"
  done
} > "$MANIFEST"

echo "    $BLOB  ($(wc -c <"$BLOB" | tr -d ' ') bytes, round trip verified)"
echo "    $MANIFEST"
echo ""
echo "Commit and push to the PRIVATE remote:"
echo "    git add $BLOB $MANIFEST && git commit -m 'backup: refresh encrypted config'"
echo "    git push private HEAD"
