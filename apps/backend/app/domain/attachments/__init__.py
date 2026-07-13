"""domain/attachments — artifact frontmatter contract + parser.

Owns the `ArtifactFrontmatter` value object (the skill↔yaaos routing-metadata
contract) and its deterministic YAML parser. Ticket-attachment storage and
delivery land in later phases; this initial slice ships only the contract so
the drift test and skill prompts can land together.
"""

from app.domain.attachments.contracts import ArtifactFrontmatter, parse_frontmatter

__all__ = [
    "ArtifactFrontmatter",
    "parse_frontmatter",
]
