"""Seed data for the fake Notion app. Mutated in-memory by write tools."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_PAGES: dict[str, dict[str, Any]] = {
    "page-1": {
        "id": "page-1",
        "title": "yaaos engineering notes",
        "content": "Working doc for the hosted-MCP integration.",
        "database_id": "db-1",
        "comments": [],
    },
    "page-2": {
        "id": "page-2",
        "title": "Sessions architecture",
        "content": "Design notes.",
        "database_id": "db-1",
        "comments": [],
    },
}

_BLOCKS: dict[str, dict[str, Any]] = {
    "block-1": {"id": "block-1", "type": "paragraph", "text": "Hosted-MCP intro."},
    "block-2": {"id": "block-2", "type": "heading_1", "text": "Architecture"},
}

_DATABASES: dict[str, list[str]] = {"db-1": ["page-1", "page-2"]}


def search(query: str) -> list[dict[str, Any]]:
    q = (query or "").lower()
    return [
        deepcopy(page)
        for page in _PAGES.values()
        if q in page["title"].lower() or q in page["content"].lower()
    ]


def query_database(db_id: str) -> list[dict[str, Any]]:
    page_ids = _DATABASES.get(db_id, [])
    return [deepcopy(_PAGES[pid]) for pid in page_ids if pid in _PAGES]


def retrieve_page(page_id: str) -> dict[str, Any] | None:
    row = _PAGES.get(page_id)
    return deepcopy(row) if row is not None else None


def retrieve_block(block_id: str) -> dict[str, Any] | None:
    row = _BLOCKS.get(block_id)
    return deepcopy(row) if row is not None else None


def update_page(page_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    if page_id not in _PAGES:
        raise KeyError(page_id)
    _PAGES[page_id].update(
        {k: v for k, v in fields.items() if k in {"title", "content"}}
    )
    return deepcopy(_PAGES[page_id])


def create_comment(page_id: str, body: str) -> dict[str, Any]:
    if page_id not in _PAGES:
        raise KeyError(page_id)
    comment = {"id": f"cmt-{len(_PAGES[page_id]['comments']) + 1}", "body": body}
    _PAGES[page_id]["comments"].append(comment)
    return deepcopy(comment)


def reset() -> None:
    """Test hook — restores in-memory state to defaults between tests."""
    global _PAGES, _BLOCKS, _DATABASES
    _PAGES = {
        "page-1": {
            "id": "page-1",
            "title": "yaaos engineering notes",
            "content": "Working doc for the hosted-MCP integration.",
            "database_id": "db-1",
            "comments": [],
        },
        "page-2": {
            "id": "page-2",
            "title": "Sessions architecture",
            "content": "Design notes.",
            "database_id": "db-1",
            "comments": [],
        },
    }
    _BLOCKS = {
        "block-1": {"id": "block-1", "type": "paragraph", "text": "Hosted-MCP intro."},
        "block-2": {"id": "block-2", "type": "heading_1", "text": "Architecture"},
    }
    _DATABASES = {"db-1": ["page-1", "page-2"]}
