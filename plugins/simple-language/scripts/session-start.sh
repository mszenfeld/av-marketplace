#!/bin/bash
# SessionStart hook: inject the simple-language rules into the session context.
# The rules apply from the first reply, without the agent having to load the skill.
# Fires on startup, resume, clear, and compact, so the rules survive compaction.

set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SKILL_FILE="${PLUGIN_ROOT}/skills/simple-language/SKILL.md"

# Never break session start: without the skill file or jq, inject nothing.
if [ ! -f "$SKILL_FILE" ]; then
  exit 0
fi
if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

# Drop the YAML frontmatter (the first "--- ... ---" block); keep the body.
# Tolerates CRLF line endings.
BODY=$(awk '
  NR == 1 && /^---\r?$/ { in_fm = 1; next }
  in_fm && /^---\r?$/   { in_fm = 0; next }
  !in_fm               { print }
' "$SKILL_FILE")

PREFIX="The simple-language plugin is active. Apply the rules below to every reply and every document you write for a human reader in this session. They govern the shape of text, not its content."

jq -n --arg ctx "${PREFIX}"$'\n\n'"${BODY}" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $ctx
  }
}'
