"""Unit test: `mint_branch_name` — pure slugify + shortid minting.

Not yet called by any ticket-creation path (that wiring lands with the
intake rewire); this is the function's own correctness coverage.
"""

from __future__ import annotations

from uuid import UUID

from app.domain.tickets import mint_branch_name

_TICKET_ID = UUID("018f2c3a-0000-7000-8000-000000000001")
_SHORTID = _TICKET_ID.hex[:8]


def test_mint_branch_name_slugifies_title() -> None:
    assert mint_branch_name("Add dark mode toggle", _TICKET_ID) == f"yaaos/add-dark-mode-toggle-{_SHORTID}"


def test_mint_branch_name_collapses_non_alnum_runs() -> None:
    assert (
        mint_branch_name("Fix: null pointer!! (again)", _TICKET_ID)
        == f"yaaos/fix-null-pointer-again-{_SHORTID}"
    )


def test_mint_branch_name_truncates_to_40_chars() -> None:
    title = "a" * 100
    result = mint_branch_name(title, _TICKET_ID)
    slug = result.removeprefix("yaaos/").rsplit("-", 1)[0]
    assert len(slug) <= 40
    assert result == f"yaaos/{'a' * 40}-{_SHORTID}"


def test_mint_branch_name_falls_back_when_title_yields_no_slug() -> None:
    assert mint_branch_name("!!!", _TICKET_ID) == f"yaaos/ticket-{_SHORTID}"
    assert mint_branch_name("", _TICKET_ID) == f"yaaos/ticket-{_SHORTID}"


def test_mint_branch_name_is_deterministic_for_same_ticket() -> None:
    assert mint_branch_name("Same title", _TICKET_ID) == mint_branch_name("Same title", _TICKET_ID)
