# Source Inventory

This note tracks the major raw data sources that have been staged for Jarvis's local brain.

Linked notes: [[00 Home]], [[03 Brain Schema]], [[04 Capture Workflow]], [[07 Import Source Hub]], [[50 Synthesis]], [[91 Vault Changelog]]

## 2026-04-15 Import Snapshot

### Claude export

- staged path: `vault/raw/imports/claude/2026-04-15-export/`
- original source: `/Users/truthseeker/Downloads/data-e2ead73c-e61d-468f-9dec-2f4afed70e9b-1776240051-67e193ea-batch-0000`
- files staged: `users.json`, `projects.json`, `memories.json`, `conversations.json`
- observed shape: 1 user record, 1 project record, 1 memory bundle, 24 conversation objects

### ChatGPT export

- staged path: `vault/raw/imports/chatgpt/2026-04-15-export/`
- original source: `/Users/truthseeker/Downloads/fe8df7223ee4f8be8a31a031ccd810cc8b987ab36f2e811fd05d4417746ef266-2026-04-14-00-12-59-7dae0aecb7454a3b99b11953144bbad2`
- files staged: `chat.html`, `export_manifest.json`, `conversations-000.json` through `conversations-004.json`
- observed shape: 5 conversation shard files, 496 conversation objects, and a much larger attachment archive in the original export folder

## Distillation Priority

1. identity and stable personal context
2. Jarvis product direction and recurring project threads
3. communication and collaboration preferences
4. timeline milestones and major decisions
5. reusable synthesis from long conversation history

The staged raw files are for provenance and extraction. The actual Jarvis brain should still be distilled into concise markdown notes rather than treated as a transcript dump.

Use this note as the provenance bridge between [[07 Import Source Hub]], `vault/raw/imports/`, [[50 Synthesis]], and the reusable memory layers like [[10 Identity]], [[20 Projects]], and [[60 Interview Story Bank]].

## Additional Local Sources Consulted

### Resume PDFs used for story extraction

- `/Users/truthseeker/Downloads/Resumes/AmanImranAISafetyTrustandSafetyResume.pdf`
- `/Users/truthseeker/Downloads/Resumes/AmanImranResume2026.pdf`
- `/Users/truthseeker/Downloads/Resumes/AmanSecurityResume.pdf`

These were used to strengthen the story bank with concrete evidence around scale, KPI ownership, calibration, distributed operations, and escalation control when the export bundles were too high-level.

## Latest Resume Source of Truth

- current primary resume: `/Users/truthseeker/Downloads/Resumes/AmanImranResume2026.pdf`
- highest-confidence updates taken from it: 7+ years of experience, Anthropic-aligned AI safety incident response, 500K+ daily events in TikTok SQL pipelines, 40 percent SQL latency reduction at LLNL, and the stronger security-operations leadership framing across 12 sites and 16 personnel

This note should stay focused on provenance and source confidence, while the conclusions move outward into [[50 Synthesis]], [[40 Timeline]], and the role-targeting notes.
