#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
#
# Update the agentic coding CLIs that Rutherford drives, on macOS / Linux / WSL.
#
# For each SUPPORTED CLI that is installed on this machine, this runs its native updater (or the npm /
# uv command that owns it), then reports the version before and after. CLIs with no safe, non-interactive
# updater are reported version-only with a one-line manual hint -- the script never guesses a destructive
# command. Anything not installed is skipped. After running this, re-verify with `just test-integration`
# and update docs/cli-maintenance.md if a version changed. See that doc for per-CLI status and known issues.
#
# Usage:
#   scripts/update-clis.sh                 # update installed CLIs, then report versions
#   scripts/update-clis.sh --check-only    # report versions only; make no changes
#   scripts/update-clis.sh --only amp,cline,pi
#   scripts/update-clis.sh --timeout 240   # per-CLI updater timeout in seconds (default 180)
set -u

CHECK_ONLY=0
ONLY=""
TIMEOUT_SEC=180
while [ $# -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1 ;;
    --only) ONLY="$2"; shift ;;
    --timeout) TIMEOUT_SEC="$2"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# Pick a timeout wrapper if available (coreutils `timeout`, or `gtimeout` on macOS via brew); else run raw.
TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_BIN="gtimeout"; fi
run_to() { if [ -n "$TIMEOUT_BIN" ]; then "$TIMEOUT_BIN" "$1" "${@:2}"; else "${@:2}"; fi; }

# id | bin | version-args | update-cmd ("-" = report-only) | manual hint
# Update commands are non-interactive; npm/uv own the packages installed through them. Native updaters are
# verified against each CLI's --help. Keep in sync with scripts/update-clis.ps1 and docs/cli-maintenance.md.
CLIS="
amp|amp|--version|amp update|
cline|cline|--version|cline update|
cn|cn|--version|npm install -g @continuedev/cli@latest|
hermes|hermes|--version|hermes update|
junie|junie|--version|-|auto-updates on launch; to force, re-run https://junie.jetbrains.com/install.sh
kilo|kilo|--version|kilo upgrade|
kimi|kimi|--version|kimi upgrade|
openhands|openhands|--version|uv tool upgrade openhands|
pi|pi|--version|pi update self|
claude_code|claude|--version|claude update|
codex|codex|--version|npm install -g @openai/codex@latest|
qwen|qwen|--version|npm install -g @qwen-code/qwen-code@latest|
copilot|copilot|--version|npm install -g @github/copilot@latest|
opencode|opencode|--version|opencode upgrade|
cursor|cursor-agent|--version|cursor-agent update|
droid|droid|--version|-|Factory droid auto-updates; reinstall from https://docs.factory.ai if needed
vibe|vibe|--version|-|update via the Mistral Vibe installer (vibe --setup / project docs)
kiro|kiro-cli|--version|-|update via the Kiro installer (https://kiro.dev/docs/cli/)
goose|goose|--version|-|update via the Block Goose installer (https://block.github.io/goose/)
antigravity|agy|--version|-|agy auto-updates; the antigravity adapter pins verified_version -- re-verify the brain/ transcript layout
ollama|ollama|--version|-|update via your package manager or the ollama.com installer (optional local model)
lmstudio|lms|version|-|LM Studio self-updates in the desktop app (optional local model)
"

cli_version() { # bin, version-args
  local out
  out="$(run_to 30 "$1" $2 2>&1 | head -n1)"
  [ -n "$out" ] && echo "$out" || echo "(no version output)"
}

printf '%-13s %-22s %-22s %s\n' "ID" "BEFORE" "AFTER" "STATUS"
printf '%s\n' "-------------------------------------------------------------------------------"

echo "$CLIS" | while IFS='|' read -r id bin vargs upd note; do
  [ -z "$id" ] && continue
  if [ -n "$ONLY" ] && ! printf '%s' ",$ONLY," | grep -q ",$id,"; then continue; fi
  if ! command -v "$bin" >/dev/null 2>&1; then
    printf '%-13s %-22s %-22s %s\n' "$id" "-" "-" "not installed"
    continue
  fi
  before="$(cli_version "$bin" "$vargs")"
  if [ "$CHECK_ONLY" = "1" ] || [ "$upd" = "-" ]; then
    status=$([ "$upd" = "-" ] && echo "report-only" || echo "check-only")
    printf '%-13s %-22s %-22s %s\n' "$id" "$before" "$before" "$status"
    [ "$upd" = "-" ] && [ -n "$note" ] && printf '              update: %s\n' "$note"
    continue
  fi
  run_to "$TIMEOUT_SEC" $upd >/dev/null 2>&1 || printf '              updater note: "%s" exited non-zero / timed out\n' "$upd"
  after="$(cli_version "$bin" "$vargs")"
  if [ "$after" = "$before" ]; then status="unchanged"; else status="updated"; fi
  printf '%-13s %-22s %-22s %s\n' "$id" "$before" "$after" "$status"
done

echo
echo "Re-verify drifted CLIs with: just test-integration  (see docs/cli-maintenance.md)"
