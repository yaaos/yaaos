from uuid import uuid4

import pytest

from app.core.audit_log import Actor, ActorKind


def test_system_actor() -> None:
    a = Actor.system()
    assert a.kind == ActorKind.SYSTEM
    assert a.login is None
    assert a.agent_id is None


def test_github_user_actor() -> None:
    a = Actor.github_user("alice")
    assert a.kind == ActorKind.GITHUB_USER
    assert a.login == "alice"


def test_agent_actor() -> None:
    aid = uuid4()
    a = Actor.agent(aid)
    assert a.kind == ActorKind.AGENT
    assert a.agent_id == aid


def test_github_user_requires_login() -> None:
    with pytest.raises(ValueError):
        Actor(kind=ActorKind.GITHUB_USER)


def test_agent_requires_id() -> None:
    with pytest.raises(ValueError):
        Actor(kind=ActorKind.AGENT)


def test_system_rejects_extras() -> None:
    with pytest.raises(ValueError):
        Actor(kind=ActorKind.SYSTEM, login="x")
