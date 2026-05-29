"""SQLAlchemy model for `domain/mcp_proxy` — `mcp_review_tokens`.

One row per active review's MCP bearer. PK is `sha256(raw_token)`; the raw
token never persists. Lifetime is `created_at + 2h`; the periodic sweep
deletes expired rows. Reviewer code calls `mint_token(review_id, org_id=...)` at
review start and `revoke_token(review_id)` at end.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class McpReviewTokenRow(Base):
    __tablename__ = "mcp_review_tokens"

    # sha256 hex of the raw bearer token. Raw never persists.
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    review_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )
    # org_id carried on the token row so the proxy reads tenancy without a
    # reviewer back-lookup. Avoids the mcp_proxy → reviewer import cycle.
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
