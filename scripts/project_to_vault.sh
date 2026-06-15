#!/bin/bash
# project_to_vault.sh — export a completed project to vault/raw/imports/.
#
# Calls project_manager to fetch the project, then asks claude to summarize
# the execution and lessons into a brain-ready capture note.
#
# Usage:
#   ./scripts/project_to_vault.sh <project_id>
#   ./scripts/project_to_vault.sh <project_id> --write   # write to vault
#   ./scripts/project_to_vault.sh --last                  # use most recent done project

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="$REPO/vault/raw/imports"
PROJECT_ID=""
WRITE_TO_VAULT=0

for arg in "$@"; do
    case "$arg" in
        --write) WRITE_TO_VAULT=1 ;;
        --last)
            PROJECT_ID=$(cd "$REPO" && python3 project_manager.py list --status done \
                2>/dev/null | grep "^proj_" | head -1 | awk '{print $1}')
            ;;
        proj_*) PROJECT_ID="$arg" ;;
    esac
done

if [[ -z "$PROJECT_ID" ]]; then
    echo "Usage: $0 <project_id|--last> [--write]"
    cd "$REPO" && python3 project_manager.py list
    exit 1
fi

echo "[project_to_vault] Fetching project $PROJECT_ID..."

PROJECT_JSON=$(cd "$REPO" && python3 -c "
import json, sys
sys.path.insert(0, '.')
import project_manager as pm
p = pm.get_project('$PROJECT_ID')
print(json.dumps(p, indent=2, default=str))
" 2>&1)

if echo "$PROJECT_JSON" | grep -q "not found\|Error\|Traceback"; then
    echo "[project_to_vault] Failed to fetch project:"
    echo "$PROJECT_JSON"
    exit 1
fi

TIMESTAMP="$(date '+%Y-%m-%d_%H%M')"

PROMPT="You are a Jarvis brain capture agent. Summarize a completed autonomous agent project into a structured vault note.

Project JSON:
\`\`\`json
$PROJECT_JSON
\`\`\`

Write a vault capture note in this exact format:

---
title: <project title>
date: $(date '+%Y-%m-%d')
tags: [agent-project, <relevant-tags>]
status: done
---

## What ran
<1-2 sentence description of what the project did>

## Tasks completed
<bullet list of tasks with their status: ✓ done / ✗ failed>

## Key findings
<3-5 bullets: what was actually discovered or changed>

## Lessons
<1-3 bullets: what this run teaches us for future automation>

## Action items
<any follow-up tasks that should be created, or NONE>

Keep it factual and terse. No prose padding."

echo "[project_to_vault] Summarizing with Claude..."
NOTE=$(claude --print "$PROMPT" 2>&1)

if [[ $WRITE_TO_VAULT -eq 1 ]]; then
    mkdir -p "$VAULT_DIR"
    NOTE_FILE="$VAULT_DIR/project_${PROJECT_ID}_${TIMESTAMP}.md"
    echo "$NOTE" > "$NOTE_FILE"
    echo "[project_to_vault] Written to: $NOTE_FILE"
else
    echo ""
    echo "=== VAULT NOTE (preview) ==="
    echo "$NOTE"
    echo ""
    echo "Run with --write to save to $VAULT_DIR"
fi
