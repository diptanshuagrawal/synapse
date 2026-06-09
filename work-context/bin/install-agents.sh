#!/usr/bin/env bash
# Install launchd agents from the generic plist templates in launchagents/.
#
# The committed templates are org-agnostic: label prefix `com.example`, paths
# `__REPO__` / `__HOME__`. This script materialises them for THIS machine —
# substituting your real launchd prefix (from config/sources.yaml) and the
# real repo + home paths — then loads them via launchctl.
set -euo pipefail

WORKCTX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # .../work-context
REPO_ROOT="$(cd "$WORKCTX/.." && pwd)"                        # repo root (replaces __REPO__)
PLIST_SRC="$WORKCTX/launchagents"
PLIST_DST="$HOME/Library/LaunchAgents"

# Real launchd label prefix from config (gitignored sources.yaml); falls back
# to the generic example prefix if config/venv are absent (fresh clone).
PREFIX="$(cd "$WORKCTX" && .venv/bin/python -c \
    'from derive.sources_config import launchd_prefix; print(launchd_prefix())' \
    2>/dev/null || echo com.example)"

SERVICES=(
    github-ingest jira-ingest confluence-ingest slack-ingest
    slack-discover leaves housekeeping codegraph
)

mkdir -p "$PLIST_DST"

for svc in "${SERVICES[@]}"; do
    tmpl="$PLIST_SRC/com.example.$svc.plist"
    label="$PREFIX.$svc"
    dst="$PLIST_DST/$label.plist"

    if [[ ! -f "$tmpl" ]]; then
        echo "SKIP $svc — template not found: $tmpl"
        continue
    fi

    # Materialise template: real label prefix + real repo/home paths.
    sed -e "s/com\.example/$PREFIX/g" \
        -e "s|__REPO__|$REPO_ROOT|g" \
        -e "s|__HOME__|$HOME|g" \
        "$tmpl" > "$dst"

    launchctl unload "$dst" 2>/dev/null || true
    launchctl load "$dst"
    echo "OK   $label"
done

echo
echo "Loaded agents:"
launchctl list | grep "$PREFIX" || echo "(none found)"
