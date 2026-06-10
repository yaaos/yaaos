"""End-to-end coverage of `apps/backend/bin/bootstrap` via subprocess.

Runs the script in a child process with stdin-piped inputs. The child shares
the same `DATABASE_URL` as the test session, so writes show up in the
already-migrated test DB. Each test commits its bootstrap output and then
cleans up to keep other tests isolated.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest

from app.core.config import get_settings
from app.core.identity import repository as identity_repo
from app.domain.orgs import repository as orgs_repo

_BIN = Path(__file__).resolve().parents[4] / "bin" / "bootstrap"


def _spawn(stdin_text: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(_BIN)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.fixture(scope="module")
def fake_github(unused_tcp_port_factory) -> str:
    """A throwaway httpx-based stub isn't suitable here because the child
    process opens its own httpx client. We instead expose the lookup URL via
    a pytest-httpserver-style sentinel: the script reads the URL from
    `YAAOS_OAUTH_GITHUB_USERINFO_LOOKUP_URL` and we point it at a
    locally-bound httpx response. For simplicity, we use a single in-process
    aiohttp-style stub — but to avoid dragging in another dep, we route
    through a single canned response served by `python -m http.server` is
    too coarse. We instead leverage `pytest-httpserver` if installed; if not,
    skip this fixture and rely on a direct env override in the test."""
    raise NotImplementedError


def _start_user_lookup_stub(monkeypatch_env, login: str, github_id: int) -> str:
    """Stub `YAAOS_OAUTH_GITHUB_USERINFO_LOOKUP_URL` to a file:// URL that
    serves the canned response. file:// scheme isn't supported by httpx, so
    we instead route through a local in-process HTTP server below."""
    raise NotImplementedError


@pytest.fixture
def github_user_lookup(monkeypatch):
    """Spin up an in-process HTTP server returning `{"id": <id>}` for
    `GET /users/<login>`. The bootstrap child reads
    `YAAOS_OAUTH_GITHUB_USERINFO_LOOKUP_URL` from its environment."""
    import threading  # noqa: PLC0415
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415

    routes: dict[str, int] = {}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def do_GET(self) -> None:
            for login, gh_id in routes.items():
                if self.path == f"/users/{login}":
                    body = f'{{"id": {gh_id}, "login": "{login}"}}'.encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def register(login: str, gh_id: int) -> None:
        routes[login] = gh_id

    monkeypatch.setenv(
        "YAAOS_OAUTH_GITHUB_USERINFO_LOOKUP_URL",
        f"http://127.0.0.1:{port}/users/{{login}}",
    )
    yield register
    server.shutdown()
    server.server_close()


def _stdin(email: str, gh_login: str, name: str, org_name: str, org_slug: str) -> str:
    return "\n".join([email, gh_login, name, org_name, org_slug]) + "\n"


@pytest.mark.asyncio
async def test_bootstrap_creates_all_rows(github_user_lookup, db_session) -> None:
    github_user_lookup("octocat-test", 9991)
    result = _spawn(_stdin("jack-bootstrap@example.com", "octocat-test", "Jack", "Acme Boot", "acme-boot"))
    assert result.returncode == 0, result.stderr
    assert "user=created" in result.stdout
    assert "oauth_identity=created" in result.stdout
    assert "org=created" in result.stdout
    assert "membership=created" in result.stdout

    # The subprocess committed outside our transactional fixture. Verify via a
    # fresh session and clean up after ourselves.
    from app.core.database import get_sessionmaker  # noqa: PLC0415

    async with get_sessionmaker()() as s:
        user = await identity_repo.find_user_by_email(s, "jack-bootstrap@example.com")
        assert user is not None
        identity = await identity_repo.find_oauth_identity(s, provider="github", external_subject="9991")
        assert identity is not None and identity.user_id == user.id
        org = await orgs_repo.get_org_by_slug(s, "acme-boot")
        assert org is not None
        membership = await orgs_repo.get_membership(s, user_id=user.id, org_id=org.org_id)
        assert membership is not None and membership.role == "owner"

        await _cleanup_user_and_org(s, user_id=user.id, org_id=org.org_id)
        await s.commit()


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent(github_user_lookup) -> None:
    github_user_lookup("octocat2", 9992)
    payload = _stdin("jack-idem@example.com", "octocat2", "Jack", "Idem", "idem-org")

    first = _spawn(payload)
    assert first.returncode == 0, first.stderr
    second = _spawn(payload)
    assert second.returncode == 0, second.stderr
    assert "user=exists" in second.stdout
    assert "oauth_identity=exists" in second.stdout
    assert "org=exists" in second.stdout
    assert "membership=exists" in second.stdout

    from app.core.database import get_sessionmaker  # noqa: PLC0415

    async with get_sessionmaker()() as s:
        user = await identity_repo.find_user_by_email(s, "jack-idem@example.com")
        assert user is not None
        org = await orgs_repo.get_org_by_slug(s, "idem-org")
        assert org is not None
        await _cleanup_user_and_org(s, user_id=user.id, org_id=org.org_id)
        await s.commit()


@pytest.mark.asyncio
async def test_bootstrap_rejects_invalid_email_then_accepts(github_user_lookup) -> None:
    github_user_lookup("octocat3", 9993)
    stdin_text = "not-an-email\n" + _stdin("jack-retry@example.com", "octocat3", "Jack", "Retry", "retry-org")
    result = _spawn(stdin_text)
    assert result.returncode == 0, result.stderr

    from app.core.database import get_sessionmaker  # noqa: PLC0415

    async with get_sessionmaker()() as s:
        user = await identity_repo.find_user_by_email(s, "jack-retry@example.com")
        assert user is not None
        org = await orgs_repo.get_org_by_slug(s, "retry-org")
        assert org is not None
        await _cleanup_user_and_org(s, user_id=user.id, org_id=org.org_id)
        await s.commit()


async def _cleanup_user_and_org(s, *, user_id, org_id) -> None:
    from app.testing.seed import delete_org, delete_user_artifacts  # noqa: PLC0415

    # Cleanup for rows committed by the bootstrap subprocess outside the
    # transactional fixture. Deleting the org cascades to its memberships;
    # owning module keeps the table names in one place.
    await delete_org(s, org_id)
    await delete_user_artifacts(s, user_id=user_id)


# Avoid the static reference to `httpx` flagging as unused.
_ = httpx
_ = get_settings
