Browser action priorities:

Open first. Then inspect current page state. Then click or navigate. Then summarize the final page.

When the request contains both navigation and summary, never summarize the search query itself. Summarize the loaded destination page.

When a page action fails, prefer deterministic recovery over apology. Examples include resolving a matching link URL from page HTML or asking the browser for the current page again before deciding the next step.
