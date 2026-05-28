"""Actor value object — who-did-what for audit rows.

Lives alongside the audit_log module since it is the row's `actor` column type,
keeping the type's ownership matched to its usage.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, model_validator


class ActorKind(StrEnum):
    GITHUB_USER = "github_user"
    AGENT = "agent"
    SYSTEM = "system"
    # Identity & access. A real yaaos user (uuid PK), a workspace
    # principal (background jobs running on behalf of an org), an SSO
    # assertion (used when an audit row's only "who" is a verified IdP).
    USER = "user"
    WORKSPACE = "workspace"
    SSO = "sso"


class Actor(BaseModel):
    """Who-did-what. One value across the codebase.

    Invariants:
      - kind=github_user → login required; user_id/agent_id/workspace_id None.
      - kind=agent → agent_id required; login/user_id/workspace_id None.
      - kind=system → all id fields None.
      - kind=user → user_id required; login optional (display login);
        agent_id/workspace_id None.
      - kind=workspace → workspace_id required; everything else None.
      - kind=sso → all id fields None (only the IdP knew); login optional.
    """

    kind: ActorKind
    login: str | None = None
    agent_id: UUID | None = None
    user_id: UUID | None = None
    workspace_id: UUID | None = None

    @model_validator(mode="after")
    def _validate(self) -> Actor:
        if self.kind == ActorKind.GITHUB_USER:
            if not self.login:
                raise ValueError("Actor(github_user) requires login")
            if self.agent_id is not None or self.user_id is not None or self.workspace_id is not None:
                raise ValueError("Actor(github_user) must carry only login")
        elif self.kind == ActorKind.AGENT:
            if self.agent_id is None:
                raise ValueError("Actor(agent) requires agent_id")
            if self.login is not None or self.user_id is not None or self.workspace_id is not None:
                raise ValueError("Actor(agent) must carry only agent_id")
        elif self.kind == ActorKind.SYSTEM:
            if any((self.login, self.agent_id, self.user_id, self.workspace_id)):
                raise ValueError("Actor(system) must not carry any id")
        elif self.kind == ActorKind.USER:
            if self.user_id is None:
                raise ValueError("Actor(user) requires user_id")
            if self.agent_id is not None or self.workspace_id is not None:
                raise ValueError("Actor(user) must not carry agent_id or workspace_id")
        elif self.kind == ActorKind.WORKSPACE:
            if self.workspace_id is None:
                raise ValueError("Actor(workspace) requires workspace_id")
            if self.login is not None or self.agent_id is not None or self.user_id is not None:
                raise ValueError("Actor(workspace) must carry only workspace_id")
        else:  # sso
            if self.agent_id is not None or self.user_id is not None or self.workspace_id is not None:
                raise ValueError("Actor(sso) carries no domain ids — IdP-only")
        return self

    @classmethod
    def system(cls) -> Actor:
        return cls(kind=ActorKind.SYSTEM)

    @classmethod
    def github_user(cls, login: str) -> Actor:
        return cls(kind=ActorKind.GITHUB_USER, login=login)

    @classmethod
    def agent(cls, agent_id: UUID) -> Actor:
        return cls(kind=ActorKind.AGENT, agent_id=agent_id)

    @classmethod
    def user(cls, user_id: UUID, login: str | None = None) -> Actor:
        return cls(kind=ActorKind.USER, user_id=user_id, login=login)

    @classmethod
    def workspace(cls, workspace_id: UUID) -> Actor:
        return cls(kind=ActorKind.WORKSPACE, workspace_id=workspace_id)

    @classmethod
    def sso(cls, login: str | None = None) -> Actor:
        return cls(kind=ActorKind.SSO, login=login)
