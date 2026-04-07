# UNIVERSAL CAREER BASE — POINTER FILE
# This file is a pointer. The full universal base lives at:
# ./Jarvis_Universal_Interview_Context.md
#
# At runtime, the harness loads the full file from that path.
# Do not duplicate the content here — maintain it in the source file only.

## SOURCE FILE
Path: kb/career/Jarvis_Universal_Interview_Context.md
Description: Universal interview intelligence — all stories, frameworks, voice, skills.
             Role-agnostic. Always loaded in InterviewIntel module.
             Layer 1 (universal) is permanently stable.
             Layer 2 (target role packs) grows as new packs are added here.

## RUNTIME LOADING INSTRUCTION
When InterviewIntel module activates:
1. Load full contents of Jarvis_Universal_Interview_Context.md (Parts 1–9 only)
2. If JARVIS_ACTIVE_ROLE is set: load kb/career/packs/{active_role}.md
3. If JARVIS_ACTIVE_COMPANY is set AND pack exists: load that pack additionally
4. Do NOT load target-role pack content into universal-only responses
