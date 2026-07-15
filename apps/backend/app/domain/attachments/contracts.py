"""ArtifactFrontmatter contract — the skill↔yaaos routing-metadata VO.

Emitted by every artifact-producing pipeline skill at the top of its
artifact body as a YAML frontmatter block. Parsed deterministically at
attach time; absence or any parse / validation failure degrades to None
(context-only attachment — the body still reaches the skill as context,
but the stage adoption matcher never matches it).
"""

from __future__ import annotations

from datetime import datetime

import yaml
from pydantic import BaseModel, ValidationError


class ArtifactFrontmatter(BaseModel, frozen=True, extra="forbid"):
    """Routing metadata emitted at the top of every pipeline-skill artifact.

    Parsed at attach time; treated as routing hints only — never trusted as
    authoritative content. `skill` is the match key for stage adoption.
    """

    yaaos_artifact_version: int
    skill: str  # producing skill name — THE adoption match key
    skill_version: str
    artifact_type: str
    produced_at: datetime
    repo_commit: str | None = None  # HEAD SHA at production time
    produced_from: str | None = None  # upstream lineage reference; carried, not verified


def parse_frontmatter(body: str) -> ArtifactFrontmatter | None:
    """Extract and validate a leading YAML frontmatter block.

    Looks for a ``---`` fence at byte 0 followed by a closing ``---`` fence.
    Returns ``None`` on absence, YAML parse failure, or model validation
    failure (unknown fields, missing required fields, wrong types). Never
    raises.
    """
    if not body.startswith("---\n"):
        return None
    rest = body[4:]  # skip opening "---\n"
    end = rest.find("\n---")
    if end == -1:
        return None
    yaml_block = rest[:end]
    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ArtifactFrontmatter.model_validate(data)
    except ValidationError:
        return None
