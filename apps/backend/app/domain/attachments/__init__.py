"""domain/attachments — attachment storage, frontmatter contract + parser.

Owns the `ArtifactFrontmatter` value object, its YAML parser, and the
`ticket_attachments` table. Attachments are user-supplied ticket inputs
(text documents), not pipeline products.
"""

import app.domain.attachments.web  # noqa: F401 — registers /api/attachments routes
from app.domain.attachments.contracts import ArtifactFrontmatter, parse_frontmatter
from app.domain.attachments.service import (
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    TicketNotFoundError,
    add_attachment,
    get_attachment,
    list_attachments,
)
from app.domain.attachments.types import Attachment, AttachmentMeta

__all__ = [
    "ArtifactFrontmatter",
    "Attachment",
    "AttachmentMeta",
    "AttachmentNotFoundError",
    "AttachmentTooLargeError",
    "TicketNotFoundError",
    "add_attachment",
    "get_attachment",
    "list_attachments",
    "parse_frontmatter",
]
