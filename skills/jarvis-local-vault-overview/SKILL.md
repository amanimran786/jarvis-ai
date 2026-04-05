Name: Jarvis Local Vault Overview

Purpose:
Use local vault knowledge about Jarvis Local Vault Overview before relying on broader model reasoning.

Rules:
- Ground answers in `wiki/overview.md` before broader reasoning.
- Ground answers in `indexes/keyword_index.md` before broader reasoning.
- Ground answers in `indexes/topics.md` before broader reasoning.
- Use only the smallest relevant snippet needed for the current request.
- If the local vault evidence is weak, say that plainly before escalating.
