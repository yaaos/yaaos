"""fake-github FastAPI service. Implements just enough GitHub endpoints to drive yaaos tests."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.seeds import (
    default_installation_repositories,
    default_seeded_diffs,
    default_seeded_files,
    default_seeded_prs,
)
from app.state import state
from app.test_secrets import APP_ID, WEBHOOK_SECRET

# Webhook secret: env var override (matches docker-compose.test.yml) or compiled default.
WEBHOOK_SECRET_BYTES = os.environ.get("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET).encode()


app = FastAPI(title="fake-github")


@app.on_event("startup")
async def _seed() -> None:
    state.seeded_prs.update(default_seeded_prs())
    state.seeded_diffs.update(default_seeded_diffs())
    state.seeded_files.update(default_seeded_files())
    if not state.installation_repositories:
        state.installation_repositories.extend(default_installation_repositories())


# ── GitHub-compatible endpoints ────────────────────────────────────────────────


def _check_bearer(authorization: str | None, prefix: str = "Bearer ") -> str:
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization[len(prefix):]


@app.get("/app")
async def get_app(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_bearer(authorization)
    return {"id": int(APP_ID), "slug": "yaaos-test"}


@app.post("/app/installations/{installation_id}/access_tokens", status_code=201)
async def get_installation_token(
    installation_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _check_bearer(authorization)
    return {
        "token": f"ghs_fake_{installation_id}_x",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


@app.get("/repos/{owner}/{repo}/pulls/{number}")
async def get_pull(
    owner: str,
    repo: str,
    number: int,
    accept: str = Header(default=""),
    authorization: str | None = Header(default=None),
) -> Response:
    _check_bearer(authorization)
    key = f"{owner}/{repo}#{number}"
    pr = state.seeded_prs.get(key)
    if pr is None:
        raise HTTPException(status_code=404, detail="not found")
    if "diff" in accept.lower():
        return PlainTextResponse(state.seeded_diffs.get(key, ""))
    return JSONResponse(pr)


@app.get("/repos/{owner}/{repo}/pulls/{number}/files")
async def get_pull_files(
    owner: str, repo: str, number: int, authorization: str | None = Header(default=None)
) -> list[dict[str, Any]]:
    _check_bearer(authorization)
    return state.seeded_files.get(f"{owner}/{repo}#{number}", [])


@app.get("/repos/{owner}/{repo}/pulls")
async def list_pulls(
    owner: str, repo: str, state_: str = "", authorization: str | None = Header(default=None)
) -> list[dict[str, Any]]:
    _check_bearer(authorization)
    prefix = f"{owner}/{repo}#"
    return [pr for k, pr in state.seeded_prs.items() if k.startswith(prefix)]


@app.get("/repos/{owner}/{repo}")
async def get_repo(
    owner: str, repo: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _check_bearer(authorization)
    return {"full_name": f"{owner}/{repo}", "default_branch": "main"}


@app.post("/repos/{owner}/{repo}/pulls/{number}/comments", status_code=201)
async def post_inline_comment(
    owner: str,
    repo: str,
    number: int,
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_bearer(authorization)
    cid = state.next_comment_id()
    state.posted_comments.append(
        {
            "id": cid,
            "owner": owner,
            "repo": repo,
            "number": number,
            "commit_id": body.get("commit_id"),
            "path": body.get("path"),
            "line": body.get("line"),
            "body": body.get("body", ""),
        }
    )
    return {"id": cid}


@app.get("/repos/{owner}/{repo}/pulls/{number}/comments")
async def list_inline_comments(
    owner: str, repo: str, number: int, authorization: str | None = Header(default=None)
) -> list[dict[str, Any]]:
    _check_bearer(authorization)
    return [
        c for c in state.posted_comments
        if c.get("owner") == owner and c.get("repo") == repo and c.get("number") == number
    ]


@app.get("/repos/{owner}/{repo}/issues/{number}/comments")
async def list_issue_comments(
    owner: str, repo: str, number: int, authorization: str | None = Header(default=None)
) -> list[dict[str, Any]]:
    _check_bearer(authorization)
    return []


@app.post("/repos/{owner}/{repo}/pulls/{number}/comments/{parent_id}/replies", status_code=201)
async def post_inline_reply(
    owner: str,
    repo: str,
    number: int,
    parent_id: str,
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_bearer(authorization)
    cid = state.next_comment_id()
    state.posted_comments.append(
        {
            "id": cid,
            "owner": owner,
            "repo": repo,
            "number": number,
            "body": body.get("body", ""),
            "in_reply_to_id": parent_id,
        }
    )
    return {"id": cid}


@app.post("/repos/{owner}/{repo}/issues/{number}/comments", status_code=201)
async def post_issue_comment(
    owner: str,
    repo: str,
    number: int,
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_bearer(authorization)
    cid = state.next_comment_id()
    state.posted_comments.append(
        {"id": cid, "owner": owner, "repo": repo, "number": number, "body": body.get("body", "")}
    )
    return {"id": cid}


@app.get("/installation/repositories")
async def installation_repositories(
    authorization: str | None = Header(default=None),
    per_page: int = 100,
) -> dict[str, Any]:
    """List repos visible to the installation. yaaos's catch-up poller and
    the Settings GitHub-card use this. The default seed returns acme/web +
    acme/api; specs can override by mutating `state.installation_repositories`
    via `/__test/reset` + seed primitives if they need a different set.
    """
    _check_bearer(authorization)
    repos = state.installation_repositories[:per_page]
    return {"total_count": len(state.installation_repositories), "repositories": repos}


@app.get("/repos/{owner}/{repo}/compare/{base_to_head:path}")
async def compare(
    owner: str, repo: str, base_to_head: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _check_bearer(authorization)
    # `base_to_head` arrives as `<before>...<after>`. Specs that want to
    # exercise the force-push branch seed `state.compare_status[base_to_head]`.
    # Specs that want to exercise base-merge detection seed
    # `state.compare_commits[base_to_head]` with commit messages.
    commit_messages = state.compare_commits.get(base_to_head, [])
    return {
        "status": state.compare_status.get(base_to_head, "ahead"),
        "commits": [{"commit": {"message": msg}} for msg in commit_messages],
    }


@app.post("/__test/seed_compare_status")
async def test_seed_compare_status(body: dict[str, Any]) -> dict[str, str]:
    """Body: `{ "base_to_head": "<before>...<after>", "status": "diverged" }`.
    Used by the force-push spec.
    """
    state.compare_status[body["base_to_head"]] = body.get("status", "diverged")
    return {"status": "seeded"}


@app.post("/__test/seed_compare_commits")
async def test_seed_compare_commits(body: dict[str, Any]) -> dict[str, str]:
    """Body: `{ "base_to_head": "<before>...<after>", "commits": ["msg1", "msg2"] }`.

    Used by specs that want the compare API to return commits between two
    SHAs — drives the incremental reviewer's base-merge detection
    (plan §7 rule 3).
    """
    state.compare_commits[body["base_to_head"]] = list(body.get("commits", []))
    return {"status": "seeded"}


# ── Test control endpoints ──────────────────────────────────────────────────


@app.post("/__test/reset")
async def test_reset() -> dict[str, str]:
    state.reset()
    state.seeded_prs.update(default_seeded_prs())
    state.seeded_diffs.update(default_seeded_diffs())
    state.seeded_files.update(default_seeded_files())
    state.installation_repositories.extend(default_installation_repositories())
    return {"status": "reset"}


@app.post("/__test/seed_pr")
async def test_seed_pr(body: dict[str, Any]) -> dict[str, str]:
    owner = body["owner"]
    repo = body["repo"]
    number = body["number"]
    state.seeded_prs[f"{owner}/{repo}#{number}"] = body["pr"]
    return {"status": "seeded"}


@app.post("/__test/seed_diff")
async def test_seed_diff(body: dict[str, Any]) -> dict[str, str]:
    owner = body["owner"]
    repo = body["repo"]
    number = body["number"]
    key = f"{owner}/{repo}#{number}"
    # `if_unset=True` only seeds when no diff already exists. Used by the
    # e2e `dispatchWebhook` auto-default so a prior explicit `seedPRDiff`
    # call from the spec wins.
    if body.get("if_unset") and key in state.seeded_diffs:
        return {"status": "noop"}
    state.seeded_diffs[key] = body.get("diff", "")
    state.seeded_files[key] = body.get("files", [])
    return {"status": "seeded"}


@app.post("/__test/dispatch_webhook")
async def test_dispatch_webhook(body: dict[str, Any]) -> dict[str, Any]:
    """HMAC-sign + POST a payload to yaaos's webhook endpoint."""
    event = body.get("event", "pull_request")
    payload = body.get("payload") or {}
    target_url = body.get("target_url")
    if not target_url:
        raise HTTPException(status_code=400, detail="target_url required")

    import json  # noqa: PLC0415

    body_bytes = json.dumps(payload, sort_keys=True).encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET_BYTES, body_bytes, hashlib.sha256).hexdigest()
    delivery_id = body.get("delivery_id", f"delivery-{state.next_comment_id()}")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sig,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(target_url, content=body_bytes, headers=headers)
    return {
        "status_code": resp.status_code,
        "delivery_id": delivery_id,
        "body": resp.text,
    }


@app.get("/__test/posted_comments")
async def test_posted_comments() -> list[dict[str, Any]]:
    return state.posted_comments
