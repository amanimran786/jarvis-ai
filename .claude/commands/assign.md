# /assign — dispatch a project from a template

Usage: `/assign <template> <target>`

Templates: security-audit, test-coverage, api-review, refactor, research

**$ARGUMENTS**

```bash
ARGS="$ARGUMENTS"
TEMPLATE=$(echo "$ARGS" | awk '{print $1}')
TARGET=$(echo "$ARGS" | cut -d' ' -f2-)

if [[ -z "$TEMPLATE" ]] || [[ -z "$TARGET" ]]; then
  echo "Usage: /assign <template> <target>"
  python3 /Users/truthseeker/jarvis-ai/project_manager.py templates
  exit 0
fi

python3 /Users/truthseeker/jarvis-ai/project_manager.py from-template \
  "$TEMPLATE" --target "$TARGET" --dispatch
```
