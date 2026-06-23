# Jarvis Context Window Strategy

Last reviewed: 2026-06-15

## Current Finding

Jarvis was configured for long local context, but the active prompt governor was
still behaving like a 12K chat system. Ollama GLM calls set `num_ctx=64000`, yet
`context_budget.target_tokens_for("chat")` and the Ollama conversation cap used
the generic 12K target. That caused old turns to be dropped earlier than needed
and made long-context local routing look weaker than the hardware/model lane can
support.

The dashboard data also showed that Jarvis is not currently running out of local
context because of raw context size. The heavier problem is repeated calls and
premature cloud escalation:

- 1.22M tokens in 24h.
- 980K local tokens.
- 237K cloud tokens.
- Context governor average target was 12K.
- Context blocks were selected, but average injected context was only about 300
  tokens because the candidate blocks are intentionally capped small.

## Repeat Recall Policy

The main token saver is not a bigger context window. It is recognizing repeated
requests, repeated mistakes, and repeated project loops before asking a model to
reason from scratch.

Jarvis now has a cheap `repeat_context` lane that runs before normal generation:

- lexical fingerprint of the request shape
- indexed vault search over lessons, context-budget notes, local skill loop, and
  prior imported chat-history distillations
- recent conversation-summary overlap
- optional mem0 episodic lookup when available
- short-lived cache so identical requests do not repeat the same retrieval work

This lane does not call a chat model. It returns a compact "Seen-before context"
block that is injected ahead of normal vault, graph, semantic-memory, and mem0
snippets. The purpose is to make Jarvis say, in effect: "we have seen this
failure/request before; use the prior lesson first, then answer only the delta."

## Policy

Use local long-context models as the first active workspace whenever the task is
not high-stakes and the operator did not explicitly request cloud.

Use cloud models for:

- Explicit cloud mode or forced model selection.
- High-stakes security, medical, legal, financial, production incident, or data
  loss work where the safety gate escalates.
- Second-opinion planning or research briefs that are converted back into local
  work orders.
- Large external-document analysis when an API key and explicit approval are
  available.

Do not treat ChatGPT/Claude/Gemini consumer subscriptions as API capacity. Jarvis
can use provider APIs only when valid API credentials and operator approval are
present. Otherwise those subscriptions are best used as manual cloud brief lanes.

## Implemented Defaults

- Local GLM prompt target: 48K tokens.
- Local Qwen long-context prompt target: 64K tokens.
- Generic cloud/default chat prompt target: 12K tokens.
- Code/shell local lanes keep at least 32K.
- Research/browser/vault lanes keep at least 24K.
- `JARVIS_CONTEXT_TARGET_TOKENS` remains the global override.
- `JARVIS_LOCAL_CONTEXT_TARGET_TOKENS` overrides local targets without expanding
  cloud prompts.
- `JARVIS_GLM_CONTEXT_TARGET_TOKENS` can tune GLM specifically.
- `JARVIS_QWEN_LONG_CONTEXT_TARGET_TOKENS` can tune Qwen long-context models.

## Operator Settings To Verify

Ollama recommends large-context agent, coding, and web-search tasks use at least
64000 tokens. Verify the runtime allocation with:

```bash
ollama ps
```

Expected for the main local model:

```text
NAME              PROCESSOR   CONTEXT
glm-4.7-flash     100% GPU    64000 or higher
```

If Ollama is not allocating enough context, start it with:

```bash
OLLAMA_CONTEXT_LENGTH=64000 ollama serve
```

Jarvis also sets GLM request options through `GLM_CTX` or
`OLLAMA_GLM_CONTEXT`, defaulting to 64000.

## Remaining Gaps

1. Context blocks are still capped small:
   - vault: 2400 chars
   - graph: 1400 chars
   - semantic memory: 1200 chars
   - mem0: 600 chars

   This is safe, but it means the larger window is mostly used for conversation
   continuity, not richer retrieved context.

2. Conversation summaries are heuristic and only about 600 chars. Jarvis needs a
   local summarizer that emits durable facts, decisions, open tasks, and files
   touched.

3. Long repo context is still pull-based. Jarvis should build a local repo map:
   symbols, file summaries, dependency edges, recent diffs, and test ownership.

4. Cloud long-context should be a deliberate brief mode, not an automatic fallback.
   The right pattern is:

   ```text
   cloud brief -> local work order -> local execution -> local verification
   ```

5. Dashboard should separate:
   - provider context limit
   - Jarvis prompt target
   - actual prompt tokens
   - retrieved context tokens
   - conversation tokens dropped
   - cloud escalation reason

## Next Work

- Add a local compaction agent that writes session summaries after every large
  task and stores them in working/project memory.
- Add a repo-map cache that lets agents pull precise context by symbol instead of
  loading whole files.
- Promote approved repeated-request patterns from `vault/sessions/lessons.md`
  into local skills so future turns need instructions, not raw chat history.
- Add a cloud-brief command that uses Gemini/OpenAI/Claude only for planning and
  emits a reviewable local work order.
- Expand retrieval caps only for trusted local lanes, with per-source token
  meters visible on the dashboard.
- Add regression tests that assert high-complexity non-high-stakes work stays
  local when `LOCAL_STRICT_FIRST` is enabled.
