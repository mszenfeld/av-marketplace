#!/bin/bash
# Black-box tests for session-start.sh.
# Runs the hook under several conditions and asserts the emitted context.
set -u
HOOK="$(cd "$(dirname "$0")/.." && pwd)/scripts/session-start.sh"
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0; FAIL=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok()   { PASS=$((PASS+1)); }
fail() { FAIL=$((FAIL+1)); printf 'FAIL: %s\n' "$1" >&2; }

# ctx_of <output> — extract additionalContext or empty string
ctx_of() { printf '%s' "$1" | jq -r '.hookSpecificOutput.additionalContext // ""' 2>/dev/null; }

# --- 1. plugin root set: valid JSON, SessionStart event, rules present, frontmatter stripped
out="$(CLAUDE_PLUGIN_ROOT="$PLUGIN_ROOT" bash "$HOOK" </dev/null)"
printf '%s' "$out" | jq -e '.hookSpecificOutput.hookEventName == "SessionStart"' >/dev/null \
  && ok || fail "hookEventName is not SessionStart"
ctx="$(ctx_of "$out")"
printf '%s' "$ctx" | grep -q '^## Reply Shape' && ok || fail "skill body missing from context"
printf '%s' "$ctx" | grep -q '^name: simple-language' && fail "frontmatter leaked into context" || ok

# --- 2. plugin root unset: falls back to the script's own directory
out="$(env -u CLAUDE_PLUGIN_ROOT bash "$HOOK" </dev/null)"
[ -n "$(ctx_of "$out")" ] && ok || fail "no context when CLAUDE_PLUGIN_ROOT is unset"

# --- 3. path with a space
cp -R "$PLUGIN_ROOT" "$TMP/with space"
out="$(CLAUDE_PLUGIN_ROOT="$TMP/with space" bash "$TMP/with space/scripts/session-start.sh" </dev/null)"
[ -n "$(ctx_of "$out")" ] && ok || fail "no context when plugin path contains a space"

# --- 4. CRLF skill file: frontmatter still stripped
cp -R "$PLUGIN_ROOT" "$TMP/crlf"
sed -i.bak 's/$/\r/' "$TMP/crlf/skills/simple-language/SKILL.md" && rm -f "$TMP/crlf/skills/simple-language/SKILL.md.bak"
out="$(CLAUDE_PLUGIN_ROOT="$TMP/crlf" bash "$HOOK" </dev/null)"
ctx="$(ctx_of "$out")"
[ -n "$ctx" ] && ok || fail "no context for CRLF skill file"
printf '%s' "$ctx" | grep -q 'name: simple-language' && fail "CRLF frontmatter leaked into context" || ok

# --- 5. missing skill file: exit 0, no output
mkdir -p "$TMP/empty"
out="$(CLAUDE_PLUGIN_ROOT="$TMP/empty" bash "$HOOK" </dev/null; echo "rc=$?")"
[ "$out" = "rc=0" ] && ok || fail "missing skill file should exit 0 with no output, got: $out"

# --- 6. missing jq: exit 0, no output, no stderr
mkdir -p "$TMP/bin"
for t in bash awk cat dirname env; do ln -sf "$(command -v $t)" "$TMP/bin/$t"; done
out="$(PATH="$TMP/bin" CLAUDE_PLUGIN_ROOT="$PLUGIN_ROOT" "$(command -v bash)" "$HOOK" </dev/null 2>&1; echo "rc=$?")"
[ "$out" = "rc=0" ] && ok || fail "missing jq should exit 0 silently, got: $out"

printf 'session-start: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
