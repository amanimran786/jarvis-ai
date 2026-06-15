#!/bin/bash
# coverage_gap.sh — run pytest coverage on a module and ask claude to fill gaps.
#
# Usage:
#   ./scripts/coverage_gap.sh project_manager
#   ./scripts/coverage_gap.sh model_router --write   # write gap tests to disk
#   ./scripts/coverage_gap.sh --dry-run project_manager

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODULE="${1:-}"
WRITE_TESTS=0
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --write) WRITE_TESTS=1 ;;
        --dry-run) DRY_RUN=1 ;;
    esac
done

if [[ -z "$MODULE" ]] || [[ "$MODULE" == --* ]]; then
    echo "Usage: $0 <module_name> [--write] [--dry-run]"
    exit 1
fi

MODULE_FILE="$REPO/${MODULE}.py"
TEST_FILE="$REPO/tests/test_${MODULE}.py"

if [[ ! -f "$MODULE_FILE" ]]; then
    echo "[coverage_gap] $MODULE_FILE not found"
    exit 1
fi

echo "[coverage_gap] Running coverage for $MODULE..."

# Run pytest with coverage, capture output.
COV_OUTPUT=$(cd "$REPO" && python3 -m pytest \
    "tests/test_${MODULE}.py" \
    --cov="$MODULE" \
    --cov-report=term-missing \
    -q 2>&1 || true)

echo "$COV_OUTPUT"

# Extract the missing-lines section.
MISSING_LINES=$(echo "$COV_OUTPUT" | grep "MISS\|missing" | head -20 || true)

MODULE_CONTENT=$(cat "$MODULE_FILE")
EXISTING_TESTS=""
if [[ -f "$TEST_FILE" ]]; then
    EXISTING_TESTS=$(cat "$TEST_FILE")
fi

PROMPT="You are a TDD specialist writing pytest tests for a Python module.

Module: $MODULE
File: ${MODULE}.py

Missing coverage lines (from pytest-cov):
${MISSING_LINES:-No data — write tests for the public interface}

Module source:
\`\`\`python
$MODULE_CONTENT
\`\`\`

Existing tests (do not repeat):
\`\`\`python
${EXISTING_TESTS:-# No existing tests}
\`\`\`

Write the narrowest possible pytest tests that cover the missing lines.
- Each test: Arrange / Act / Assert pattern
- Name: test_<exact_behavior>
- Mock only external I/O (subprocess, DB, network)
- No integration tests — unit only
- Output: ready-to-paste pytest code, no prose"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "=== PROMPT ==="
    echo "$PROMPT"
    exit 0
fi

echo ""
echo "[coverage_gap] Asking Claude for gap tests..."
GAP_TESTS=$(claude --print "$PROMPT" 2>&1)

if [[ $WRITE_TESTS -eq 1 ]] && [[ -f "$TEST_FILE" ]]; then
    BACKUP="${TEST_FILE}.bak_$(date +%s)"
    cp "$TEST_FILE" "$BACKUP"
    echo "" >> "$TEST_FILE"
    echo "# --- coverage_gap.sh additions ---" >> "$TEST_FILE"
    echo "$GAP_TESTS" >> "$TEST_FILE"
    echo "[coverage_gap] Tests appended to $TEST_FILE (backup: $BACKUP)"
else
    echo ""
    echo "=== SUGGESTED TESTS ==="
    echo "$GAP_TESTS"
    echo ""
    echo "Run with --write to append to $TEST_FILE"
fi
