"""fake-github FastAPI service. Implements just enough GitHub endpoints to drive yaaos tests."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from app.git_backend import bootstrap_repos, router as git_router
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

# Git HTTP smart-protocol routes for ProvisionWorkspace clone + push support.
# Mounted before the API routes so /{owner}/{repo}/info/refs doesn't clash
# with the PR/repo REST routes (those all use deeper paths).
app.include_router(git_router)


@app.on_event("startup")
async def _seed() -> None:
    state.seeded_prs.update(default_seeded_prs())
    state.seeded_diffs.update(default_seeded_diffs())
    state.seeded_files.update(default_seeded_files())
    if not state.installation_repositories:
        state.installation_repositories.extend(default_installation_repositories())
    # Create bare git repos for the scenario repos used by cross-plane e2e specs.
    bootstrap_repos()


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


# Default account login returned by `GET /app/installations/{id}` for any
# installation id minted by the picker stub. Specs that need a different
# value seed `state.installations[id]` directly or via `/__test/`.
_DEFAULT_INSTALL_ACCOUNT_LOGIN = "acme-org"


@app.get("/app/installations/{installation_id}")
async def get_installation(
    installation_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    """Payload `app.plugins.github.service.fetch_install_account_login` reads
    during the install callback. Returns the seeded `account.login` for the
    given install id, falling back to the default so unknown ids (e.g. from
    `seedCredentialsAndInstall` which writes "fake-install-1" directly into
    yaaos's DB) still resolve."""
    _check_bearer(authorization)
    login = state.installations.get(installation_id, _DEFAULT_INSTALL_ACCOUNT_LOGIN)
    return {"id": int(installation_id) if installation_id.isdigit() else 0, "account": {"login": login}}


# ── OAuth user-auth (Sign in with GitHub) ──────────────────────────────────


@app.get("/login/oauth/authorize")
async def oauth_authorize(
    client_id: str = Query(...),
    state: str = Query(..., alias="state"),
    redirect_uri: str = Query(...),
    allow_signup: str = Query(default="false"),
) -> RedirectResponse:
    """Stub for GitHub's OAuth authorize page. In real GitHub the user lands
    on a consent screen; here we auto-approve and 302 straight back to
    `redirect_uri` with a freshly minted `code`.

    `client_id` is not validated — fake-github accepts whatever yaaos's
    config carries. Tests that need a specific user response stage it via
    `/__test/stage_oauth_user`.

    The `state` Query param shadows the module-level singleton inside this
    function, so we reach the singleton via its global module name.
    """
    del client_id, allow_signup
    from app.state import state as _store  # noqa: PLC0415

    code = f"oauth-code-{_store.next_oauth_code()}"
    _store.oauth_codes[code] = dict(_store.default_oauth_user)
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


@app.post("/login/oauth/access_token")
async def oauth_access_token(request: Request) -> JSONResponse:
    """Trade an authorize code for an access token. Returns the captured
    user profile id alongside so the subsequent `/user` + `/user/emails`
    calls resolve against the same identity."""
    form = await request.form()
    code = form.get("code") or ""
    if not code or code not in state.oauth_codes:
        return JSONResponse(status_code=400, content={"error": "bad_verification_code"})
    return JSONResponse(content={"access_token": f"gha_user_{code}", "token_type": "bearer", "scope": ""})


@app.get("/user")
async def oauth_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _check_bearer(authorization)
    # Token shape `gha_user_<code>` → look up the staged user. Falls back to
    # the current default so direct hits (no preceding authorize call) still
    # work for ad-hoc tests.
    user = None
    if token.startswith("gha_user_"):
        user = state.oauth_codes.get(token[len("gha_user_"):])
    if user is None:
        user = dict(state.default_oauth_user)
    return {"id": user["id"], "login": user["login"], "name": user.get("name", "")}


@app.get("/user/emails")
async def oauth_user_emails(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    token = _check_bearer(authorization)
    user = None
    if token.startswith("gha_user_"):
        user = state.oauth_codes.get(token[len("gha_user_"):])
    if user is None:
        user = dict(state.default_oauth_user)
    return [{"email": user["primary_email"], "primary": True, "verified": True}]


@app.post("/__test/stage_oauth_user")
async def test_stage_oauth_user(body: dict[str, Any]) -> dict[str, str]:
    """Pin the user the NEXT `/login/oauth/authorize` returns. Body matches
    the `default_oauth_user` shape: `{id, login, name, primary_email}`."""
    state.default_oauth_user = {
        "id": int(body.get("id", 90001)),
        "login": body.get("login", "yaaos-owner"),
        "name": body.get("name", ""),
        "primary_email": body.get("primary_email", "owner@yaaos.test"),
    }
    return {"status": "staged"}


# ── GitHub App install picker ──────────────────────────────────────────────


@app.get("/apps/{slug}/installations/new")
async def install_picker(
    slug: str,
    signed_state: str | None = Query(default=None, alias="state"),
) -> RedirectResponse:
    """Stub for the GitHub-hosted install picker UI. The real picker asks the
    operator to choose an account + repos; here we mint a new installation id
    immediately and 302 back to yaaos's `/api/github/install_callback` with
    the same signed `state` the start endpoint produced.

    `slug` is unused (only one App slug is ever seeded — "yaaos-test") but kept
    in the URL so the path mirrors GitHub's shape and any future spec that
    wants to assert on it has a hook."""
    del slug
    if not signed_state:
        raise HTTPException(status_code=400, detail="missing state")
    install_id = str(state.next_installation_id())
    state.installations[install_id] = _DEFAULT_INSTALL_ACCOUNT_LOGIN
    callback_base = os.environ.get("YAAOS_CALLBACK_BASE_URL", "http://yaaos:8080")
    target = f"{callback_base}/api/github/install_callback?installation_id={install_id}&state={signed_state}"
    return RedirectResponse(target, status_code=302)


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
    owner: str,
    repo: str,
    state_: str = Query(default="", alias="state"),
    head: str = "",
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    """`head` (when given) is GitHub's `owner:branch` filter — used by
    `create_pr`'s idempotency fallback to find the existing open PR for a
    head branch after a 422 create."""
    _check_bearer(authorization)
    prefix = f"{owner}/{repo}#"
    prs = [pr for k, pr in state.seeded_prs.items() if k.startswith(prefix)]
    if state_:
        prs = [pr for pr in prs if pr.get("state") == state_]
    if head:
        _, _, want_branch = head.partition(":")
        prs = [pr for pr in prs if (pr.get("head") or {}).get("ref") == want_branch]
    return prs


@app.post("/repos/{owner}/{repo}/pulls", status_code=201)
async def create_pull(
    owner: str,
    repo: str,
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> Response:
    """Open a PR. Mirrors GitHub's real idempotency signal: a second create
    for a head branch that already has an open PR returns 422 Validation
    Failed — callers fall back to `GET .../pulls?head=...` to find it."""
    _check_bearer(authorization)
    head_branch = body.get("head", "")
    prefix = f"{owner}/{repo}#"
    existing = [
        pr
        for k, pr in state.seeded_prs.items()
        if k.startswith(prefix) and pr.get("state") == "open" and (pr.get("head") or {}).get("ref") == head_branch
    ]
    if existing:
        return JSONResponse(
            status_code=422,
            content={
                "message": "Validation Failed",
                "errors": [
                    {
                        "resource": "PullRequest",
                        "code": "custom",
                        "message": f"A pull request already exists for {owner}:{head_branch}.",
                    }
                ],
            },
        )
    number = state.next_pr_number()
    now = datetime.now(timezone.utc).isoformat()
    pr = {
        "number": number,
        "title": body.get("title", ""),
        "body": body.get("body"),
        "draft": False,
        "merged": False,
        "state": "open",
        "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
        "user": {"login": state.app_bot_login, "type": "Bot"},
        "head": {"ref": head_branch, "sha": f"head-sha-{owner}-{repo}-{number}", "repo": {"fork": False}},
        "base": {"ref": body.get("base", ""), "sha": f"base-sha-{owner}-{repo}-{number}"},
        "created_at": now,
        "updated_at": now,
    }
    state.seeded_prs[f"{owner}/{repo}#{number}"] = pr
    return JSONResponse(status_code=201, content=pr)


@app.post("/repos/{owner}/{repo}/pulls/{number}/reviews", status_code=200)
async def submit_review(
    owner: str,
    repo: str,
    number: int,
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Submit a review — used by `approve_pr` with `event="APPROVE"`. Reviews
    submitted here are always attributed to the app's bot login, matching
    real GitHub's "review as the App" behavior. The request's `event` field
    ("APPROVE"/"REQUEST_CHANGES"/"COMMENT") maps to GitHub's past-tense
    `state` field ("APPROVED"/"CHANGES_REQUESTED"/"COMMENTED") the way real
    GitHub's response does — same mapping `has_active_approval` expects."""
    _check_bearer(authorization)
    key = f"{owner}/{repo}#{number}"
    event_to_state = {
        "APPROVE": "APPROVED",
        "REQUEST_CHANGES": "CHANGES_REQUESTED",
        "COMMENT": "COMMENTED",
    }
    review = {
        "id": state.next_review_id(),
        "user": {"login": state.app_bot_login, "type": "Bot"},
        "state": event_to_state.get(body.get("event", "COMMENT"), "COMMENTED"),
        "body": body.get("body", ""),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    state.reviews.setdefault(key, []).append(review)
    return review


@app.get("/repos/{owner}/{repo}/pulls/{number}/reviews")
async def list_reviews(
    owner: str, repo: str, number: int, authorization: str | None = Header(default=None)
) -> list[dict[str, Any]]:
    _check_bearer(authorization)
    return state.reviews.get(f"{owner}/{repo}#{number}", [])


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
    # Every inline review comment anchors its own review thread — mirrors
    # real GitHub closely enough for `resolve_finding_thread`'s GraphQL
    # lookup (one comment per finding; no multi-comment threads in tests).
    thread_id = f"PRRT_{cid}"
    state.review_threads[thread_id] = {
        "pr_key": f"{owner}/{repo}#{number}",
        "comment_ids": [cid],
        "resolved": False,
    }
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


# ── GraphQL shim (review-thread resolution only) ────────────────────────────


@app.post("/graphql")
async def graphql(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Minimal GraphQL shim — only the two operations `resolve_finding_thread`
    needs. Dispatches by string-matching the operation name in `query`
    rather than parsing GraphQL, since this fake never needs a general
    GraphQL engine.
    """
    _check_bearer(authorization)
    body = await request.json()
    query = body.get("query", "")
    variables = body.get("variables") or {}

    if "resolveReviewThread" in query:
        thread_id = variables.get("threadId", "")
        thread = state.review_threads.get(thread_id)
        if thread is None:
            return {"errors": [{"message": f"thread {thread_id} not found"}]}
        thread["resolved"] = True
        return {"data": {"resolveReviewThread": {"thread": {"id": thread_id, "isResolved": True}}}}

    if "reviewThreads" in query:
        owner = variables.get("owner", "")
        repo = variables.get("repo", "")
        number = variables.get("number")
        pr_key = f"{owner}/{repo}#{number}"
        nodes = [
            {
                "id": tid,
                "isResolved": t["resolved"],
                "comments": {"nodes": [{"databaseId": cid} for cid in t["comment_ids"]]},
            }
            for tid, t in state.review_threads.items()
            if t["pr_key"] == pr_key
        ]
        return {
            "data": {
                "repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}},
            }
        }

    return {"errors": [{"message": "unsupported operation"}]}


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
