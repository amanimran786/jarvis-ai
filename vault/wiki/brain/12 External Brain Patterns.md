---
type: synthesis
area: vault
owner: jarvis
write_policy: curated
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-16
updated: 2026-04-16
version: 1
tags:
  - obsidian
  - agents
  - context
  - memory
related:
  - "[[03 Brain Schema]]"
  - "[[04 Capture Workflow]]"
  - "[[08 Coding Systems Hub]]"
  - "[[80 Jarvis Roadmap]]"
  - "[[91 Vault Changelog]]"
---

# External Brain Patterns

Purpose: preserve the strongest ideas worth borrowing from public Obsidian, memory, and agent repos without importing their platform assumptions blindly.

## Highest-Value Patterns

- `context packs`
  Borrowed from tools like Obsidian Copilot, basic-memory style working sets, and modular-context workflows.
  Jarvis should use explicit, generated markdown packs built from seed notes and nearby linked notes instead of giant retrieval blobs.

- `targeted note mutation`
  Borrowed from MCP Obsidian style patch semantics.
  Jarvis should keep writing relative to headings, frontmatter, or bounded note surfaces instead of broad whole-note rewrites.

- `markdown-native memory structure`
  Borrowed from basic-memory and second-brain repos.
  Jarvis should prefer human-readable markdown with stable structure over hidden database-only memory.

- `git-backed safety and history`
  Borrowed from Obsidian Git and second-brain workflows.
  Git is a safety layer for the vault, but not a substitute for inbox, candidate, and promotion rules.

- `deterministic health and visualization`
  Borrowed from visual-skills and second-brain repos.
  Jarvis should generate deterministic `.canvas` maps and maintenance reports rather than relying on opaque plugin state.

## Things To Avoid

- plugin-first core logic
- autonomous multi-note rewriting as a default ingest behavior
- cloud-native assumptions in the core brain path
- UI-heavy agent shells before the markdown contract is strong
- broad skill sprawl without a clear write policy and proof model

## Current Jarvis Cut

The cleanest immediate adoption is:

- generated context packs under `vault/indexes/context_packs/`
- bounded note mutation through `vault_edit.py`
- candidate and inbox staging for anything non-trivial
- deterministic health and graph support instead of more agent freedom

## Source Repos

- [khoj-ai/khoj](https://github.com/khoj-ai/khoj)
- [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills)
- [Gitlawb/openclaude](https://github.com/Gitlawb/openclaude)
- [logancyang/obsidian-copilot](https://github.com/logancyang/obsidian-copilot)
- [kepano/obsidian-skills](https://github.com/kepano/obsidian-skills)
- [Vinzent03/obsidian-git](https://github.com/Vinzent03/obsidian-git)
- [MarkusPfundstein/mcp-obsidian](https://github.com/MarkusPfundstein/mcp-obsidian)
- [YishenTu/claudian](https://github.com/YishenTu/claudian)
- [basicmachines-co/basic-memory](https://github.com/basicmachines-co/basic-memory)
- [axtonliu/axton-obsidian-visual-skills](https://github.com/axtonliu/axton-obsidian-visual-skills)
- [huytieu/COG-second-brain](https://github.com/huytieu/COG-second-brain)
- [eugeniughelbur/obsidian-second-brain](https://github.com/eugeniughelbur/obsidian-second-brain)
- [klemensgc/modular-context-obsidian-plugin](https://github.com/klemensgc/modular-context-obsidian-plugin)
