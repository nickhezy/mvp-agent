#!/bin/bash
# claude_wrapper.sh — bridge between the generic watchdog and claude -p.
#
# Usage from the watchdog:
#   --worker-cmd "bash examples/claude_wrapper.sh /abs/path/to/worker"
#   --supervisor-cmd "bash examples/claude_wrapper.sh /abs/path/to/supervisor"
#
# The watchdog sets two env vars per invocation:
#   MVP_AGENT_RUN_DIR        absolute path to the run root
#   MVP_AGENT_WAKE_ENVELOPE  JSON wake envelope
#
# This wrapper:
#   1. Persists the envelope as a file so the agent can read it cleanly
#      (avoids JSON-in-shell-quotes brittleness).
#   2. On first invocation per harness: spawns `claude -p ... --output-format json`,
#      captures session_id from stdout, stores it under
#      <run_dir>/session_ids/<harness_name>.txt.
#   3. On subsequent invocations: spawns `claude -p ... --resume <session_id>`.
#
# Exits with claude's exit code. stdout/stderr are passed through (the
# watchdog redirects them to logs/<harness_name>.<n>.{out,err}).

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <harness_dir>" >&2
    exit 2
fi

HARNESS_DIR="$(cd "$1" && pwd)"
HARNESS_NAME="$(basename "$HARNESS_DIR")"

: "${MVP_AGENT_RUN_DIR:?must be set by the watchdog}"
: "${MVP_AGENT_WAKE_ENVELOPE:?must be set by the watchdog}"

RUN_DIR="$MVP_AGENT_RUN_DIR"
SESSION_DIR="$RUN_DIR/session_ids"
SESSION_FILE="$SESSION_DIR/${HARNESS_NAME}.txt"
ENVELOPE_DIR="$RUN_DIR/wake_envelopes"
ENVELOPE_FILE="$ENVELOPE_DIR/${HARNESS_NAME}.json"

mkdir -p "$SESSION_DIR" "$ENVELOPE_DIR"

# Persist the envelope atomically so the agent can read it without quoting hazards.
printf '%s' "$MVP_AGENT_WAKE_ENVELOPE" > "${ENVELOPE_FILE}.tmp"
mv "${ENVELOPE_FILE}.tmp" "$ENVELOPE_FILE"

# Common claude flags.
CLAUDE_FLAGS=(
    "--output-format" "json"
    "--permission-mode" "bypassPermissions"
)

if [[ -f "$SESSION_FILE" ]]; then
    SESSION_ID="$(cat "$SESSION_FILE")"
    # Resume: terse prompt, the agent already knows the harness from its conversation context.
    PROMPT="Wake. New wake envelope written to: $ENVELOPE_FILE. Read it and act."
    exec claude -p "$PROMPT" --resume "$SESSION_ID" "${CLAUDE_FLAGS[@]}"
fi

# First invocation: bootstrap prompt that points the agent at the harness, the run dir,
# and the envelope file. Capture session_id from claude's JSON stdout.
PROMPT="Read $HARNESS_DIR/task-overview.md (the entire FSM and contract for this harness) and act on it.

Your run root: $RUN_DIR
Your current wake envelope: $ENVELOPE_FILE  (read it; you have been woken with wake_reason=initial)

This is the first invocation of this session. Subsequent wakes will be delivered via the same envelope file (the path will not change), with a new envelope written each time."

# We need to capture stdout (for session_id parsing) AND pass it through.
# Tee to a temp file, then parse session_id from the captured copy.
TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_OUT"' EXIT
set +e
claude -p "$PROMPT" "${CLAUDE_FLAGS[@]}" | tee "$TMP_OUT"
rc=${PIPESTATUS[0]}
set -e

SESSION_ID="$(jq -r '.session_id // empty' "$TMP_OUT" 2>/dev/null || true)"
if [[ -z "$SESSION_ID" ]]; then
    echo "claude_wrapper: failed to capture session_id from claude's output" >&2
    echo "claude_wrapper: stdout was:" >&2
    cat "$TMP_OUT" >&2
    # Still exit with claude's actual rc; the watchdog will see a non-zero exit
    # and the lack of a session_ids/ file and treat the next dispatch as another first.
    exit "$rc"
fi

# Persist session_id atomically so a crashed write doesn't poison future resumes.
printf '%s' "$SESSION_ID" > "${SESSION_FILE}.tmp"
mv "${SESSION_FILE}.tmp" "$SESSION_FILE"

exit "$rc"
