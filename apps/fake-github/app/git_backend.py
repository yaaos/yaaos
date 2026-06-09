"""Git HTTP smart-protocol backend for fake-github.

Creates bare git repos at startup for each e2e scenario repo and serves
`git clone` requests via `git http-backend` (CGI). Routes are mounted into
the main FastAPI app by main.py.

Repos served:
  - acme/review-happy.git
  - acme/review-nonconforming.git
  - acme/review-agentfail.git

HEAD SHAs are accessible via `GET /__test/git_head_sha/{owner}/{repo}` so
e2e specs can build PR payloads that reference a real commit SHA.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

# Root directory for all bare repos. Lives under the container's tmp so it's
# wiped on each container restart (which is fine — tests rebuild the stack).
_REPOS_ROOT = Path(os.environ.get("FAKE_GITHUB_REPOS_DIR", "/tmp/fake-github-repos"))

# Repos to create at startup. Each is a bare git repo at
# `{root}/{owner}/{repo}.git`.
_SCENARIO_REPOS = [
    ("acme", "review-happy"),
    ("acme", "review-nonconforming"),
    ("acme", "review-agentfail"),
]

router = APIRouter()


def _repo_path(owner: str, repo: str) -> Path:
    """Return the filesystem path to the bare git repo for {owner}/{repo}."""
    # Normalise: strip trailing .git so callers can pass either form.
    repo_name = repo.removesuffix(".git")
    return _REPOS_ROOT / owner / f"{repo_name}.git"


def _run(args: list[str], *, cwd: str | Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True)


def _git_head_sha(bare_path: Path) -> str:
    """Return the SHA of HEAD in the bare repo."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(bare_path),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def bootstrap_repos() -> None:
    """Create and initialise all scenario bare repos.

    Called once from the FastAPI startup event. Idempotent: if the repo
    already exists (container restart), the existing repo is left untouched
    so the HEAD SHA stays stable across restarts within one stack session.
    """
    _REPOS_ROOT.mkdir(parents=True, exist_ok=True)

    for owner, repo in _SCENARIO_REPOS:
        bare_path = _repo_path(owner, repo)
        if bare_path.exists():
            # Already initialised — idempotent.
            continue

        bare_path.mkdir(parents=True, exist_ok=True)

        # Initialise bare repo.
        _run(["git", "init", "--bare", "--initial-branch=main", str(bare_path)])

        # Use a temp workdir to create an initial commit, then push to the bare.
        with tempfile.TemporaryDirectory() as workdir:
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "fake-github",
                "GIT_AUTHOR_EMAIL": "fake@github.test",
                "GIT_COMMITTER_NAME": "fake-github",
                "GIT_COMMITTER_EMAIL": "fake@github.test",
                "GIT_AUTHOR_DATE": "2024-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2024-01-01T00:00:00+00:00",
            }
            _run(["git", "init", "--initial-branch=main", workdir], env=env)
            _run(["git", "config", "user.email", "fake@github.test"], cwd=workdir, env=env)
            _run(["git", "config", "user.name", "fake-github"], cwd=workdir, env=env)

            # Write a tiny placeholder so the commit isn't empty.
            readme = Path(workdir) / "README.md"
            readme.write_text(f"# {owner}/{repo}\nFake repo for e2e tests.\n")
            _run(["git", "add", "."], cwd=workdir, env=env)
            _run(["git", "commit", "-m", "initial commit"], cwd=workdir, env=env)

            # Push to our bare repo.
            _run(
                ["git", "remote", "add", "origin", str(bare_path)],
                cwd=workdir,
                env=env,
            )
            _run(
                ["git", "push", "origin", "main"],
                cwd=workdir,
                env=env,
            )


# ── Git HTTP smart protocol routes ────────────────────────────────────────────


def _invoke_git_http_backend(
    bare_path: Path,
    method: str,
    path_info: str,
    query_string: str,
    content_type: str,
    body: bytes,
    git_service: str = "",
) -> tuple[int, dict[str, str], bytes]:
    """Invoke git http-backend as a CGI subprocess and return (status, headers, body).

    git http-backend speaks CGI: it reads GIT_HTTP_EXPORT_ALL, PATH_INFO,
    QUERY_STRING, REQUEST_METHOD, CONTENT_TYPE, and the request body from stdin.
    It writes a CGI response to stdout (headers, blank line, body).
    """
    env = {
        "GIT_PROJECT_ROOT": str(_REPOS_ROOT),
        "GIT_HTTP_EXPORT_ALL": "1",
        "PATH_INFO": path_info,
        "QUERY_STRING": query_string,
        "REQUEST_METHOD": method,
        "CONTENT_TYPE": content_type,
        "GIT_SERVICE": git_service,
        # Required by CGI — prevents git from trying to load the user's config.
        "HOME": "/tmp",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if method == "POST":
        env["CONTENT_LENGTH"] = str(len(body))

    result = subprocess.run(
        ["git", "http-backend"],
        input=body,
        capture_output=True,
        env=env,
    )

    raw = result.stdout
    # Split headers from body at the first blank line.
    split_pos = raw.find(b"\r\n\r\n")
    if split_pos == -1:
        split_pos = raw.find(b"\n\n")
        if split_pos == -1:
            return 500, {}, b""
        header_bytes = raw[:split_pos]
        response_body = raw[split_pos + 2:]
    else:
        header_bytes = raw[:split_pos]
        response_body = raw[split_pos + 4:]

    headers: dict[str, str] = {}
    status = 200
    for line in header_bytes.decode("utf-8", errors="replace").splitlines():
        if ": " in line:
            k, _, v = line.partition(": ")
            k = k.strip()
            v = v.strip()
            if k.lower() == "status":
                try:
                    status = int(v.split()[0])
                except (ValueError, IndexError):
                    pass
            else:
                headers[k] = v

    return status, headers, response_body


@router.get("/{owner}/{repo}/info/refs")
async def git_info_refs(
    owner: str,
    repo: str,
    request: Request,
    service: str = "",
) -> Response:
    """Smart HTTP discovery: `GET /{owner}/{repo}.git/info/refs?service=git-upload-pack`.

    The agent's `git clone` hits this first. We only support `git-upload-pack`
    (read-only clone/fetch); push is not needed.
    """
    repo_name = repo.removesuffix(".git")
    bare_path = _repo_path(owner, repo_name)
    if not bare_path.exists():
        return Response(status_code=404, content=b"not found")

    # path_info must include the full /{owner}/{repo}.git/info/refs so
    # git http-backend can locate the repo relative to GIT_PROJECT_ROOT.
    path_info = f"/{owner}/{repo_name}.git/info/refs"
    query = str(request.url.query)

    status, headers, body = _invoke_git_http_backend(
        bare_path,
        method="GET",
        path_info=path_info,
        query_string=query,
        content_type="",
        body=b"",
        git_service=service,
    )
    return Response(content=body, status_code=status, headers=headers)


@router.post("/{owner}/{repo}/git-upload-pack")
async def git_upload_pack(owner: str, repo: str, request: Request) -> Response:
    """Smart HTTP data transfer: `POST /{owner}/{repo}.git/git-upload-pack`.

    The agent's `git clone` / `git fetch` hits this after discovery.
    """
    repo_name = repo.removesuffix(".git")
    bare_path = _repo_path(owner, repo_name)
    if not bare_path.exists():
        return Response(status_code=404, content=b"not found")

    body = await request.body()
    path_info = f"/{owner}/{repo_name}.git/git-upload-pack"
    content_type = request.headers.get("content-type", "")

    status, headers, resp_body = _invoke_git_http_backend(
        bare_path,
        method="POST",
        path_info=path_info,
        query_string="",
        content_type=content_type,
        body=body,
    )
    return Response(content=resp_body, status_code=status, headers=headers)


@router.get("/__test/git_head_sha/{owner}/{repo}")
async def get_git_head_sha(owner: str, repo: str) -> dict[str, Any]:
    """Return the HEAD SHA of the bare repo for {owner}/{repo}.

    E2e specs use this to build `headSha` in PR payloads so the agent's
    `git checkout --detach <sha>` succeeds against the real bare repo.
    """
    repo_name = repo.removesuffix(".git")
    bare_path = _repo_path(owner, repo_name)
    if not bare_path.exists():
        return {"error": f"repo {owner}/{repo} not found", "sha": ""}
    sha = _git_head_sha(bare_path)
    return {"owner": owner, "repo": repo_name, "sha": sha}
