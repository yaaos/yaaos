"""Generic stage-prompt renderer for pipeline skill invocations.

Renders a `StageInvocationContext` mapping (plus the engine-injected
`output_schema` key) into the headless prompt that a pipeline skill stage
runs against. This is the vocabulary layer: every concept rendered here
(stages, artifacts, revisions, findings, the output contract) is
`domain/pipelines` vocabulary, so the renderer lives here rather than in
a plugin. Plugins call it from `compile_invocation`, supplying only the
vendor-specific skill directive.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from app.core.coding_agent import CodingAgentError


def render_stage_prompt(
    context: Mapping[str, Any],
    *,
    skill_directive: str,
    extra_directives: Sequence[str] = (),
    output_schema_mode: Literal["prompt", "native"] = "prompt",
) -> str:
    """Render the generic stage-invocation prompt from a context mapping.

    `context` is the plugin-received `Invocation.context` dict — a
    `StageInvocationContext` dump plus the engine-injected `output_schema`
    key. Required keys are `stage_name`, `input`, and `artifact_path`.
    Raises `CodingAgentError` when any required key is missing.

    `skill_directive` is a vendor-specific first line pointing the agent at
    the skill file, e.g.
    `'Use the "requirements" skill (.claude/skills/requirements/SKILL.md) ...'`.

    `extra_directives` are additional vendor addenda appended right after
    the skill directive (codex uses this for a delegation-authorization
    sentence). Empty by default.

    `output_schema_mode` controls whether the strict JSON output schema
    instruction is appended:
    - `"prompt"` (default) — embed the schema and strict-JSON directive
      (Claude Code model).
    - `"native"` — omit (vendor enforces output format via a CLI flag).
    """
    required = {"stage_name", "input", "artifact_path"}
    missing = required - set(context)
    if missing:
        raise CodingAgentError(f"render_stage_prompt: context missing required keys: {sorted(missing)}")

    lines: list[str] = [skill_directive]
    lines.extend(extra_directives)

    stage_name = context.get("stage_name")
    ticket_id = context.get("ticket_id")
    header = f"\nStage: {stage_name}" if stage_name else ""
    if header and ticket_id:
        header += f" (ticket {ticket_id})"
    if header:
        lines.append(header)

    lines.append("\n## Input\n")
    lines.append(str(context.get("input") or ""))

    attachments = context.get("attachments") or []
    if attachments:
        lines.append("\n## Attachments\n")
        for att in attachments:
            path = att.get("path", "")
            artifact_type = att.get("artifact_type") or "—"
            produced_by = att.get("produced_by_skill") or "—"
            role = att.get("role", "context")
            note = att.get("note")
            entry = f"- `{path}` · type: {artifact_type} · skill: {produced_by} · role: {role}"
            if note:
                entry += f" · {note}"
            lines.append(entry)

    pr = context.get("pr")
    if pr:
        lines.append("\n## Pull request\n")
        lines.append(f"- PR: {pr['pr_external_id']}")
        lines.append(f"- Base SHA: {pr['base_sha']}")
        lines.append(f"- Head SHA: {pr['head_sha']}")
        prev = pr.get("prev_reviewed_head_sha")
        lines.append(f"- Previously reviewed head SHA: {prev or 'none (first review)'}")
        diff_base = prev or pr["base_sha"]
        lines.append(f"\nRun `git diff {diff_base}..{pr['head_sha']}` to inspect the change.")

    upstream_stages = context.get("upstream_stages") or []
    if upstream_stages:
        lines.append("\n## Upstream artifacts\n")
        for stage in upstream_stages:
            lines.append(f"### {stage['stage_name']} — {stage['description']}\n")
            lines.append(stage["artifact_body"])

    revision = context.get("revision")
    if revision:
        source_label = {
            "instruction": "Human instruction",
            "send_back": "Send-back gap",
            "fix": "Fix request",
        }.get(revision["source"], revision["source"])
        lines.append(f"\n## Revision ({source_label})\n")
        lines.append(revision["text"])
        lines.append("\n### Prior artifact\n")
        lines.append(revision["prior_artifact"])

    prior_findings = context.get("prior_findings") or []
    if prior_findings:
        lines.append("\n## Prior findings\n")
        for finding in prior_findings:
            if finding.get("code_file"):
                loc = f" ({finding['code_file']}:{finding.get('code_line') or '?'})"
            elif finding.get("artifact_section"):
                loc = f" ({finding['artifact_section']})"
            else:
                loc = ""
            lines.append(f"- [{finding['finding_id']}] [{finding['severity']}]{loc} {finding['body']}")

    artifact_path = context.get("artifact_path")
    lines.append("\n## Output\n")
    if artifact_path:
        lines.append(f"Write your artifact output to `{artifact_path}`.\n")

    if output_schema_mode == "prompt":
        schema_str = json.dumps(context.get("output_schema", {}), indent=2)
        lines.append(
            "Respond with EXACTLY a JSON object matching this schema. No markdown fences. "
            "No commentary. No preamble. Your response must start with `{` and end with `}`.\n\n"
            f"{schema_str}"
        )
    return "\n".join(lines)


__all__ = ["render_stage_prompt"]
