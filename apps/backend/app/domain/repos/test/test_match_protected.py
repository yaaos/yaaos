"""Unit tests for `domain/repos.match_protected` — the pure per-repo
protected-code path-matching rule the boundary evaluator composes with
`get_settings` via `evaluate_protected`."""

from __future__ import annotations

from uuid import uuid4

from app.domain.repos import ProtectedPathSet, match_protected

_OWNER_A = uuid4()
_OWNER_B = uuid4()


def _set(globs: list[str], owners: list) -> ProtectedPathSet:
    return ProtectedPathSet(id=uuid4(), globs=tuple(globs), owner_user_ids=tuple(owners))


class TestDenyMode:
    def test_no_paths_never_matches(self) -> None:
        result = match_protected([], mode="deny", path_sets=[_set(["infra/**"], [_OWNER_A])])
        assert result.matched is False
        assert result.owner_user_ids == ()

    def test_path_hitting_no_set_does_not_match(self) -> None:
        result = match_protected(["src/app.py"], mode="deny", path_sets=[_set(["infra/**"], [_OWNER_A])])
        assert result.matched is False
        assert result.owner_user_ids == ()

    def test_path_hitting_a_set_matches_with_its_owners(self) -> None:
        result = match_protected(["infra/prod.tf"], mode="deny", path_sets=[_set(["infra/**"], [_OWNER_A])])
        assert result.matched is True
        assert result.owner_user_ids == (_OWNER_A,)

    def test_owners_union_across_every_matched_set(self) -> None:
        sets = [_set(["infra/**"], [_OWNER_A]), _set(["billing/**"], [_OWNER_B])]
        result = match_protected(["infra/prod.tf", "billing/charge.py"], mode="deny", path_sets=sets)
        assert result.matched is True
        assert set(result.owner_user_ids) == {_OWNER_A, _OWNER_B}

    def test_unmatched_set_owners_excluded(self) -> None:
        sets = [_set(["infra/**"], [_OWNER_A]), _set(["billing/**"], [_OWNER_B])]
        result = match_protected(["infra/prod.tf"], mode="deny", path_sets=sets)
        assert result.matched is True
        assert result.owner_user_ids == (_OWNER_A,)

    def test_zero_sets_never_matches(self) -> None:
        result = match_protected(["anything.py"], mode="deny", path_sets=[])
        assert result.matched is False
        assert result.owner_user_ids == ()


class TestAllowMode:
    def test_no_paths_never_matches(self) -> None:
        result = match_protected([], mode="allow", path_sets=[_set(["src/**"], [_OWNER_A])])
        assert result.matched is False

    def test_path_inside_the_allowed_set_does_not_match(self) -> None:
        result = match_protected(["src/app.py"], mode="allow", path_sets=[_set(["src/**"], [_OWNER_A])])
        assert result.matched is False
        assert result.owner_user_ids == ()

    def test_path_escaping_every_set_matches(self) -> None:
        result = match_protected(["infra/prod.tf"], mode="allow", path_sets=[_set(["src/**"], [_OWNER_A])])
        assert result.matched is True
        assert result.owner_user_ids == (_OWNER_A,)

    def test_owners_union_across_all_sets_regardless_of_which_escaped(self) -> None:
        sets = [_set(["src/**"], [_OWNER_A]), _set(["docs/**"], [_OWNER_B])]
        result = match_protected(["infra/prod.tf"], mode="allow", path_sets=sets)
        assert result.matched is True
        assert set(result.owner_user_ids) == {_OWNER_A, _OWNER_B}

    def test_zero_sets_protects_everything_with_no_owners(self) -> None:
        result = match_protected(["anything.py"], mode="allow", path_sets=[])
        assert result.matched is True
        assert result.owner_user_ids == ()

    def test_one_path_escaping_is_enough_even_if_others_are_covered(self) -> None:
        sets = [_set(["src/**"], [_OWNER_A])]
        result = match_protected(["src/app.py", "infra/prod.tf"], mode="allow", path_sets=sets)
        assert result.matched is True
        assert result.owner_user_ids == (_OWNER_A,)
