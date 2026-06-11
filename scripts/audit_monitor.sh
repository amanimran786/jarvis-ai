#!/usr/bin/env bash
# Continuous pipeline truthfulness monitor.
#
# Re-audits ~/.jarvis/verifier_verdicts.jsonl every time it changes (heartbeat
# every --interval seconds), appending a tamper-evident record to
# ~/.jarvis/pipeline_audit.jsonl. CRITICAL findings (fabrication slipped the
# verifier, score/pass forgery, or an edited/deleted audit record) are printed
# loudly so an operator or a wrapping process can escalate.
#
# Usage:
#   scripts/audit_monitor.sh                  # foreground, 30s interval
#   INTERVAL=60 scripts/audit_monitor.sh      # custom interval
#   nohup scripts/audit_monitor.sh >> ~/.jarvis/audit_monitor.log 2>&1 &   # detached
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
INTERVAL="${INTERVAL:-30}"
exec python3 -m infra.pipeline_audit --watch --interval "$INTERVAL"
