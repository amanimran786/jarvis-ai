# Jarvis Decision Log

Purpose: preserve product and architecture decisions so Jarvis can compound instead of relearning the same lessons.

Linked notes: [[20 Projects]], [[50 Synthesis]], [[80 Jarvis Roadmap]], [[90 Task Hub]], [[91 Vault Changelog]]

Use this note for:

- architectural decisions
- product-direction choices
- retrieval and memory policy
- voice and UI direction
- packaging and runtime constraints

## Decision Template

### Decision Name

- Date:
- Decision:
- Why:
- Tradeoffs:
- Files or systems affected:
- Revisit when:

## Current Decisions

### Distilled brain notes outrank raw transcript imports

- Date: 2026-04-15
- Decision: Vault retrieval should prefer `vault/wiki/brain/` over `vault/raw/imports/` when relevance is otherwise similar.
- Why: Long-term Jarvis quality depends on curated, human-readable, stable knowledge winning over noisy transcript dumps.
- Tradeoffs: Raw provenance stays searchable, but raw archives should no longer dominate retrieval just because they are large.
- Files or systems affected: `vault.py`, `vault/wiki/brain/`, `vault/raw/imports/`
- Revisit when: retrieval becomes too lossy and we need better hierarchical ranking or note linking.

### Obsidian-compatible markdown is the long-term brain surface

- Date: 2026-04-15
- Decision: Use the local vault as the shared source of truth for both Jarvis and Obsidian rather than creating a separate hidden memory store.
- Why: Human-readable notes are easier to maintain, verify, edit, and grow over time.
- Tradeoffs: Distillation work is required; raw exports cannot simply be dumped in and expected to become good memory.
- Files or systems affected: `vault/README.md`, `vault/wiki/brain/`, `vault/raw/imports/`
- Revisit when: the note volume grows enough that we need stronger indexing, backlinks, or structured metadata.

### Local-first is a product constraint, not a fallback mode

- Date: 2026-04-15
- Decision: Jarvis should be optimized around local execution, privacy, inspectability, and zero API cost.
- Why: This is one of the strongest stable product preferences and a core differentiator.
- Tradeoffs: Local model quality and packaging complexity remain real constraints.
- Files or systems affected: model routing, voice runtime, local STT, local TTS, packaging.
- Revisit when: a feature cannot realistically meet product standards without a hybrid approach.

### Packaged macOS behavior must be treated as first-class

- Date: 2026-04-15
- Decision: Changes affecting voice, UI, STT, TTS, and system action should be validated against the packaged app, not just the source tree.
- Why: The frozen app has repeatedly behaved differently from source-only runs.
- Tradeoffs: Slower iteration and more build overhead.
- Files or systems affected: `Jarvis.spec`, `ui.py`, `voice.py`, local runtime modules, install script.
- Revisit when: packaging and source parity become much more reliable.

### Jarvis should feel like an operator, not a generic chatbot

- Date: 2026-04-15
- Decision: Product direction should favor a command-center assistant with strong voice, memory, action competence, and calm execution.
- Why: This aligns with the stated long-term north star and produces a clearer product identity.
- Tradeoffs: Higher expectations for polish, coordination, and real-world usefulness.
- Files or systems affected: UI, voice, prompt style, memory, tool routing, proactive behavior.
- Revisit when: day-to-day utility and reliability are slipping behind aesthetic ambition.

### Promote raw transcripts only when they add durable leverage

- Date: 2026-04-15
- Decision: A raw transcript or export fragment should become a durable brain note only if it improves identity grounding, project continuity, reusable stories, or product decisions.
- Why: The vault should compound signal, not archive every interesting sentence.
- Tradeoffs: Some potentially useful details stay in raw imports until they prove recurring value.
- Files or systems affected: `vault/raw/imports/`, `vault/wiki/brain/`, story bank notes, decision notes.
- Revisit when: retrieval starts missing useful but lightly repeated source material.

### Leave evidence gaps open instead of inventing polish

- Date: 2026-04-15
- Decision: If a desired story or memory layer lacks verified evidence, keep it partial and label the gap instead of writing a cleaner but weaker fiction.
- Why: Jarvis should become more grounded over time, not more fluent at laundering uncertainty into false confidence.
- Tradeoffs: Some notes will stay unfinished longer, and the vault may feel less complete in the short term.
- Files or systems affected: `vault/wiki/brain/60 Interview Story Bank.md`, future synthesis notes, career support prompts.
- Revisit when: stronger source material arrives and the gap can be closed honestly.

### Proactive by default should mean quiet and high-confidence

- Date: 2026-04-15
- Decision: Jarvis should stay mostly reactive by default, with proactive behavior limited to high-confidence, high-value moments such as reminders, active task follow-through, or obvious continuity prompts.
- Why: An always-interjecting assistant feels noisy fast, especially in a desktop product that already has voice, memory, and system presence.
- Tradeoffs: Jarvis may feel less magical in the short term, but it will earn trust instead of spending it.
- Files or systems affected: proactive routines, voice prompts, notifications, task follow-up behavior, future automation defaults.
- Revisit when: proactive signals become accurate enough that interruption cost is consistently low.

### Technical companion depth should live in reusable playbooks, not only scattered transcripts

- Date: 2026-04-15
- Decision: Strengthen Jarvis's engineering brain through reusable notes for debugging, systems design, threat modeling, and AI runtime behavior instead of relying on scattered chat history or one-off examples.
- Why: Long-term technical quality compounds better when Jarvis can reuse stable frameworks across many problems rather than retrieving isolated fragments that happen to sound smart.
- Tradeoffs: The vault needs more deliberate curation, and the playbooks must still be grounded in real runtime behavior instead of becoming generic advice.
- Files or systems affected: `vault/wiki/brain/75 Debugging Root Cause Playbook.md`, `vault/wiki/brain/76 Systems Design Tradeoff Heuristics.md`, `vault/wiki/brain/77 Threat Modeling Security Thinking.md`, `vault/wiki/brain/78 AI Runtime Agent Engineering Principles.md`, future retrieval and prompt grounding.
- Revisit when: the playbooks become too abstract and need tighter runtime integration or more domain-specific variants.

### Jarvis owns the vault contract; Obsidian plugins stay optional

- Date: 2026-04-15
- Decision: Adopt a plugin-optional vault operating layer based on metadata discipline, markdown tasks, deterministic templates, JSON Canvas, and explicit provenance instead of depending on plugin runtimes.
- Why: The best public Obsidian repos point toward strong conventions, but Jarvis still needs a brain it can read and write safely as plain markdown.
- Tradeoffs: Some dashboards will stay simpler than a full plugin-heavy Obsidian setup, but the vault stays durable and local-first.
- Files or systems affected: `vault/wiki/brain/03 Brain Schema.md`, `vault/wiki/brain/04 Capture Workflow.md`, `vault/wiki/brain/91 Vault Changelog.md`, `vault/templates/`, `CLAUDE.md`
- Revisit when: Jarvis needs deeper Obsidian-native interaction than markdown, tasks, and `.canvas` can provide safely.

## Next Decisions To Capture

- when a raw transcript should become a durable note
- how much self-improvement automation should remain active
