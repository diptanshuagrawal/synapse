# Copy to steno.local.sh (gitignored) and set your real per-machine values.
# Sourced by bin/meet-record. Keep the REAL values out of the tracked repo.
#
# CODESIGN_ID — the `codesign --identifier` the capture binary's TCC
# Screen-Recording grant is tied to on this Mac. It must stay STABLE across
# rebuilds, or macOS silently drops the grant and system-audio capture goes
# silent. Pick any reverse-DNS id, then grant Screen Recording to the built
# binary once; never change it afterwards.
CODESIGN_ID="com.example.meet-record-capture"
