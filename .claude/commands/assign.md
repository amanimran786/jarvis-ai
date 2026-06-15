# /assign — dispatch a project from a template or custom task list

Usage:
- `/assign <template> <target>` — dispatch a named template
- `/assign custom <title> <tasks-json>` — dispatch with explicit parallel tasks JSON

Templates: security-audit, test-coverage, api-review, refactor, research

**$ARGUMENTS**

```bash
ARGS="$ARGUMENTS"
MODE=$(echo "$ARGS" | awk '{print $1}')

if [[ -z "$MODE" ]]; then
  python3 /Users/truthseeker/jarvis-ai/project_manager.py templates
  exit 0
fi

if [[ "$MODE" == "custom" ]]; then
  # Usage: /assign custom "Project Title" '[{"prompt":"...","depends_on":[]},...]'
  TITLE=$(echo "$ARGS" | awk '{print $2}')
  TASKS_JSON=$(echo "$ARGS" | cut -d' ' -f3-)
  if [[ -z "$TITLE" ]] || [[ -z "$TASKS_JSON" ]]; then
    echo "Usage: /assign custom <title> <tasks-json-array>"
    echo "Example: /assign custom \"My Project\" '[{\"prompt\":\"do X\",\"depends_on\":[]},{\"prompt\":\"do Y\",\"depends_on\":[]}]'"
    exit 1
  fi
  python3 /Users/truthseeker/jarvis-ai/project_manager.py create "$TITLE" --tasks-json "$TASKS_JSON" --dispatch
else
  TEMPLATE="$MODE"
  TARGET=$(echo "$ARGS" | cut -d' ' -f2-)
  if [[ -z "$TARGET" ]]; then
    python3 /Users/truthseeker/jarvis-ai/project_manager.py templates
    exit 0
  fi
  python3 /Users/truthseeker/jarvis-ai/project_manager.py from-template "$TEMPLATE" --target "$TARGET" --dispatch
fi
```
