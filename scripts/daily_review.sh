#!/bin/bash
# daily_review.sh — headless security + quality scan using claude --print.
#
# Scans changed Python files since yesterday (or --all for full repo),
# then writes a structured report to vault/raw/imports/.
#
# Usage:
#   ./scripts/daily_review.sh              # files changed in last 24h
#   ./scripts/daily_review.sh --all        # full repo scan (slower)
#   ./scripts/daily_review.sh --since HEAD~5  # custom git ref
#   ./scripts/daily_review.sh --dry-run    # print prompt only, no claude call

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$REPO/vault/raw/imports"
TIMESTAMP="$(date '+%Y-%m-%d_%H%M')"
REPORT_FILE="$REPORT_DIR/security_review_${TIMESTAMP}.md"
DRY_RUN=0
SCAN_ALL=0
SINCE_REF=""

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --all) SCAN_ALL=1 ;;
        --since) shift; SINCE_REF="$1" ;;
        --since=*) SINCE_REF="${arg#--since=}" ;;
    esac
done

# Collect files to review.
if [[ $SCAN_ALL -eq 1 ]]; then
    FILES=$(find "$REPO" -name "*.py" \
        ! -path "*/venv/*" ! -path "*/__pycache__/*" ! -path "*/.git/*" \
        | sort)
elif [[ -n "$SINCE_REF" ]]; then
    FILES=$(git -C "$REPO" diff --name-only "$SINCE_REF" -- '*.py' | \
        sed "s|^|$REPO/|" | xargs -I{} test -f {} && echo {} || true)
else
    # Default: files touched in last 24h (by git or mtime).
    FILES=$(git -C "$REPO" diff --name-only HEAD@{1.day.ago}..HEAD -- '*.py' 2>/dev/null | \
        sed "s|^|$REPO/|" || true)
    if [[ -z "$FILES" ]]; then
        FILES=$(find "$REPO" -name "*.py" -mtime -1 \
            ! -path "*/venv/*" ! -path "*/__pycache__/*" ! -path "*/.git/*" \
            | sort)
    fi
fi

if [[ -z "$FILES" ]]; then
    echo "[daily_review] No Python files changed in the last 24h. Nothing to review."
    exit 0
fi

FILE_COUNT=$(echo "$FILES" | wc -l | tr -d ' ')
echo "[daily_review] Reviewing $FILE_COUNT file(s)..."

# Build a compact file listing for the prompt (relative paths + line counts).
FILE_MANIFEST=""
while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    rel="${f#$REPO/}"
    lines=$(wc -l < "$f" | tr -d ' ')
    FILE_MANIFEST+="  $rel ($lines lines)\n"
done <<< "$FILES"

PROMPT="You are a security and code quality reviewer for the Jarvis AI codebase (local-first macOS assistant).

Review the following recently changed Python files for:
1. Security issues: shell injection, hardcoded secrets, unsafe subprocess, path traversal, unsafe deserialization
2. Silent failures: swallowed exceptions, missing logging, dangerous fallbacks
3. Test coverage gaps: new logic without tests

Files to review:
$(printf '%b' "$FILE_MANIFEST")

For each file, read it and report findings in this format:
## <filename>
**Security**: <findings or CLEAN>
**Silent Failures**: <findings or CLEAN>
**Test Gaps**: <findings or NONE DETECTED>

At the end, output a ## Summary section with:
- Total issues found (by severity: CRITICAL / WARNING / INFO)
- Top 3 action items

Be concise. Each finding should be one line with file:line reference."

if [[ $DRY_RUN -eq 1 ]]; then
    echo "=== PROMPT ==="
    echo "$PROMPT"
    echo "=== END PROMPT ==="
    exit 0
fi

mkdir -p "$REPORT_DIR"

echo "# Daily Security Review — $TIMESTAMP" > "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "**Files scanned**: $FILE_COUNT" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"

# Run claude --print with the prompt (non-interactive, exits when done).
claude --print "$PROMPT" >> "$REPORT_FILE" 2>&1
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
    echo "[daily_review] claude exited with code $EXIT_CODE — check $REPORT_FILE"
    exit $EXIT_CODE
fi

echo "[daily_review] Report written to: $REPORT_FILE"

# Extract summary line count as a quick sanity check.
ISSUE_LINE=$(grep -c "CRITICAL\|WARNING" "$REPORT_FILE" || true)
echo "[daily_review] Lines mentioning issues: $ISSUE_LINE"
