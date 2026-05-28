"""Seed data for the fake Linear app. Mutated in-memory by write tools."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_ISSUES: dict[str, dict[str, Any]] = {
    "ENG-1": {
        "id": "ENG-1",
        "title": "Add MCP context to coding agents",
        "description": "Ship hosted-MCP wiring for Linear + Notion.",
        "state": "in_progress",
        "assignee": "alice@example.com",
        "comments": [],
    },
    "ENG-2": {
        "id": "ENG-2",
        "title": "Refactor sessions to support workspace principals",
        "description": "Sessions follow-up.",
        "state": "done",
        "assignee": None,
        "comments": [],
    },
}

_PROJECTS: list[dict[str, Any]] = [
    {"id": "proj-1", "name": "yaaos", "state": "started"},
    {"id": "proj-2", "name": "Infrastructure cleanup", "state": "backlog"},
]

_CYCLES: list[dict[str, Any]] = [
    {"id": "cyc-1", "name": "Sprint 23", "starts_at": "2026-05-15"},
    {"id": "cyc-2", "name": "Sprint 22", "starts_at": "2026-05-01"},
]


def get_issue(issue_id: str) -> dict[str, Any] | None:
    row = _ISSUES.get(issue_id)
    return deepcopy(row) if row is not None else None


def search_issues(query: str) -> list[dict[str, Any]]:
    q = (query or "").lower()
    return [
        deepcopy(row)
        for row in _ISSUES.values()
        if q in row["title"].lower() or q in row["description"].lower()
    ]


def list_projects() -> list[dict[str, Any]]:
    return [deepcopy(p) for p in _PROJECTS]


def list_cycles() -> list[dict[str, Any]]:
    return [deepcopy(c) for c in _CYCLES]


def update_issue(issue_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    if issue_id not in _ISSUES:
        raise KeyError(issue_id)
    _ISSUES[issue_id].update(
        {k: v for k, v in fields.items() if k in {"title", "description", "state", "assignee"}}
    )
    return deepcopy(_ISSUES[issue_id])


def create_comment(issue_id: str, body: str) -> dict[str, Any]:
    if issue_id not in _ISSUES:
        raise KeyError(issue_id)
    comment = {"id": f"cmt-{len(_ISSUES[issue_id]['comments']) + 1}", "body": body}
    _ISSUES[issue_id]["comments"].append(comment)
    return deepcopy(comment)


def reset() -> None:
    """Test hook — restores in-memory state to defaults between tests."""
    global _ISSUES, _PROJECTS, _CYCLES
    _ISSUES = {
        "ENG-1": {
            "id": "ENG-1",
            "title": "Add MCP context to coding agents",
            "description": "Ship hosted-MCP wiring for Linear + Notion.",
            "state": "in_progress",
            "assignee": "alice@example.com",
            "comments": [],
        },
        "ENG-2": {
            "id": "ENG-2",
            "title": "Refactor sessions to support workspace principals",
            "description": "Sessions follow-up.",
            "state": "done",
            "assignee": None,
            "comments": [],
        },
    }
