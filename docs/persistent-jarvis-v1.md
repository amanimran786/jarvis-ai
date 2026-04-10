# Persistent Jarvis v1 Runbook

This runbook defines the operating contract for Persistent Jarvis v1:

- the daemon owns task execution
- webhook endpoints only submit verified work into the daemon
- task state survives process restarts through a persistent task database

## Architecture

Persistent Jarvis v1 should run as a single daemon-owned execution plane:

- the daemon starts the local API and task runtime
- UI, CLI, and webhooks submit tasks into the daemon instead of executing work inline
- webhook handlers verify signatures first, then persist a task record, then return quickly
- workers stream events from the persisted task record until the task reaches a terminal state

Recommended flow:

1. Request hits `/webhooks/trigger` or `/webhooks/github`.
2. Signature is verified with `JARVIS_WEBHOOK_SECRET`.
3. A task is written to the database at `JARVIS_TASK_DB_PATH`.
4. The daemon picks up the task and executes it.
5. Task status and events remain available after restarts.

## Required Environment

| Variable | Purpose |
| --- | --- |
| `JARVIS_TASK_DB_PATH` | Absolute path to the persistent task database. Put this on durable local storage, not `/tmp`. |
| `JARVIS_WEBHOOK_SECRET` | Shared secret used to sign generic trigger payloads and verify GitHub webhook signatures. |
| `JARVIS_ALLOW_UNSIGNED_WEBHOOKS` | Optional dev-only override. Keep unset/`0` in production so webhook ingress is fail-closed. |

Example:

```bash
export JARVIS_TASK_DB_PATH="$HOME/.jarvis/tasks.db"
export JARVIS_WEBHOOK_SECRET="replace-with-a-long-random-secret"
```

## Webhook Contract

### `POST /webhooks/trigger`

Use this for internal automation, cron, shortcuts, or other trusted systems.

Expected request shape:

```json
{
  "prompt": "Summarize the latest incident thread and open a follow-up task if needed.",
  "kind": "task",
  "source": "webhook",
  "assigned_agent_id": "chat-router",
  "meta": {
    "trigger": "cron",
    "runbook": "persistent-jarvis-v1"
  }
}
```

Sign the raw request body with HMAC-SHA256 and send:

- `Content-Type: application/json`
- `X-Jarvis-Signature: sha256=<hex-digest>` (or `X-Jarvis-Signature-256`)

Example:

```bash
BODY='{"prompt":"Summarize the latest incident thread and open a follow-up task if needed.","kind":"task","source":"webhook","assigned_agent_id":"chat-router","meta":{"trigger":"cron","runbook":"persistent-jarvis-v1"}}'
SIG=$(printf '%s' "$BODY" | python3 -c 'import hashlib,hmac,os,sys; secret=os.environ["JARVIS_WEBHOOK_SECRET"].encode(); body=sys.stdin.buffer.read(); print("sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest())')

curl -X POST http://127.0.0.1:8765/webhooks/trigger \
  -H 'Content-Type: application/json' \
  -H "X-Jarvis-Signature: $SIG" \
  --data "$BODY"
```

### `POST /webhooks/github`

Use this for GitHub App or repository webhooks. The daemon should map the incoming GitHub event into a normal Jarvis task after signature verification.

Expected headers:

- `Content-Type: application/json`
- `X-GitHub-Event: <event-name>`
- `X-Hub-Signature-256: sha256=<hex-digest>`

Signature generation example for local testing:

```bash
BODY='{"action":"opened","repository":{"full_name":"owner/repo"},"pull_request":{"number":123}}'
SIG=$(printf '%s' "$BODY" | python3 -c 'import hashlib,hmac,os,sys; secret=os.environ["JARVIS_WEBHOOK_SECRET"].encode(); body=sys.stdin.buffer.read(); print("sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest())')

curl -X POST http://127.0.0.1:8765/webhooks/github \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: pull_request' \
  -H "X-Hub-Signature-256: $SIG" \
  --data "$BODY"
```

## Operational Notes

### Restarts

- Keep the daemon as the only writer to `JARVIS_TASK_DB_PATH`.
- On startup, the daemon should load any non-terminal tasks before accepting new webhook traffic.
- Prefer fast webhook acknowledgements. Do not perform long model calls inside the webhook request itself.

### Stale Task Handling

Treat stale tasks explicitly after restart:

- `queued`: safe to resume normally
- `running`: mark stale if the previous daemon heartbeat disappeared mid-execution
- `succeeded`, `failed`, `cancelled`: never replay automatically

Safe production default:

1. Mark old `running` tasks as `stale`.
2. Requeue only idempotent tasks automatically.
3. Require operator review before replaying code, browser, or external-side-effect tasks.

### Secret Rotation

- Rotating `JARVIS_WEBHOOK_SECRET` invalidates old webhook signatures immediately.
- Rotate by updating the environment, then restarting the daemon cleanly.

### Backups

- Back up `JARVIS_TASK_DB_PATH` regularly if webhook-triggered work matters operationally.
- Snapshot before schema changes or daemon upgrades.

## Practical Checks

Basic health check:

```bash
curl http://127.0.0.1:8765/status
```

Task visibility after a webhook-triggered submission:

```bash
curl http://127.0.0.1:8765/tasks
```

If the daemon restarts and tasks disappear, treat that as a persistence failure first and inspect `JARVIS_TASK_DB_PATH` before debugging webhook delivery.
