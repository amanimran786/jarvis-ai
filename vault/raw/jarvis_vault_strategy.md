# Jarvis Vault Strategy

Related brain notes: [[04 Capture Workflow]], [[70 Jarvis Decision Log]], [[78 AI Runtime Agent Engineering Principles]], [[80 Jarvis Roadmap]], [[91 Vault Changelog]], [[93 Vault Maintenance]]

Jarvis should use a local markdown vault before it grows prompt context or relies on long conversational carry-over.

The vault should stay inspectable and cheap. Raw source material belongs in one place, compiled topic pages belong in another, and cross-topic indexes should be regenerated from local files instead of being hidden in model context.

When a request can be answered from local markdown, Jarvis should load only the smallest relevant snippets for that request. The vault is not a giant prompt dump. It is a searchable local knowledge layer.

## Strategy Connections

- [[04 Capture Workflow]] defines how raw material should become durable notes without skipping review
- [[70 Jarvis Decision Log]] is where retrieval and memory-policy changes should be recorded
- [[78 AI Runtime Agent Engineering Principles]] is the runtime lens for keeping vault reads explicit and cheap
- [[80 Jarvis Roadmap]] is where this strategy becomes product direction instead of just storage structure
- [[91 Vault Changelog]] and [[93 Vault Maintenance]] are the provenance and upkeep layer that keep the strategy self-sustaining
