---
name: example
version: 1
model: anthropic:claude-haiku-4-5
temperature: 0.1
max_tokens: 256
---
<system>
You are a test prompt. Always reply with the verdict the user asks for.
</system>
<user>
The subject is {{ subject }}.
Verdict please.
</user>
