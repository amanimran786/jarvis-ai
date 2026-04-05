# Jarvis Vault Strategy

Jarvis should use a local markdown vault before it grows prompt context or relies on long conversational carry-over.

The vault should stay inspectable and cheap. Raw source material belongs in one place, compiled topic pages belong in another, and cross-topic indexes should be regenerated from local files instead of being hidden in model context.

When a request can be answered from local markdown, Jarvis should load only the smallest relevant snippets for that request. The vault is not a giant prompt dump. It is a searchable local knowledge layer.
