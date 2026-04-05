Name: Browser Execution

Purpose:
Turn browser requests into concrete page actions. Prefer opening the target page, reading the actual page state, clicking or recovering the requested navigation, and then summarizing the resulting page.

Rules:
- Treat browser requests as tool-execution tasks, not generic conversation.
- When the user chains actions, preserve order exactly.
- If a click fails through JavaScript automation, recover through link resolution or another deterministic browser action.
- When summarizing a page, summarize the page the browser actually landed on.
- Keep spoken summaries short and natural because Jarvis reads them aloud.
