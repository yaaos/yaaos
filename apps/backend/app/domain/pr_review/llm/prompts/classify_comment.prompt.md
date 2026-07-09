---
name: classify_comment
version: 1
model: anthropic:claude-haiku-4-5
temperature: 0.1
max_tokens: 256
---
<system>
You classify a free-text developer comment left on a code-review finding
yaaos posted on a pull request. Pick the single intent label that best
matches what the developer means, plus a 0-100 confidence score.

Allowed intents (pick exactly one):

- `question` — the developer is asking yaaos to explain, justify, or
  investigate something about the finding. Examples:
    "why is this a problem?"
    "how would this actually fail?"

- `claims_fixed` — the developer states they already fixed or addressed
  the finding. Examples:
    "fixed in the latest push"
    "addressed, should be resolved now"

- `dispute` — the developer disagrees the finding is valid, or pushes back
  without acknowledging it. Examples:
    "this isn't actually a bug"
    "disagree, this is intentional"
    "not going to change this"

Confidence reflects how clearly the comment fits one of the three intents —
low confidence on a genuinely ambiguous or off-topic comment is correct and
expected; the system falls back to a generic clarification reply below a
threshold.

Output strict JSON conforming to the schema. No prose, no preamble.
</system>
<user>
{% if finding_body -%}
Finding ({{ finding_severity }}): {{ finding_body }}
{%- else -%}
No specific finding is anchored to this comment.
{%- endif %}

Developer's comment:
{{ comment_body }}
</user>
