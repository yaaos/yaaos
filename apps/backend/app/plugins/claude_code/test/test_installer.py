"""Subagent installer — verify each yaaos-* file is written with valid frontmatter."""

from pathlib import Path

import pytest

from app.plugins.claude_code import installer


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_installs_all_reviewers(fake_home: Path) -> None:
    written = installer.install_subagents()
    assert written == len(installer._REVIEWERS)
    agents_dir = fake_home / ".claude" / "agents"
    for _, (subagent_name, _) in installer._REVIEWERS.items():
        assert (agents_dir / f"{subagent_name}.md").is_file()


def test_files_start_with_frontmatter(fake_home: Path) -> None:
    installer.install_subagents()
    agents_dir = fake_home / ".claude" / "agents"
    for _, (subagent_name, description) in installer._REVIEWERS.items():
        content = (agents_dir / f"{subagent_name}.md").read_text()
        assert content.startswith("---\n")
        assert f"name: {subagent_name}\n" in content
        assert f"description: {description}\n" in content
        # Body present after frontmatter.
        assert content.count("---\n") >= 2


def test_idempotent(fake_home: Path) -> None:
    first = installer.install_subagents()
    second = installer.install_subagents()
    assert first == second


def test_leaves_unrelated_files_alone(fake_home: Path) -> None:
    agents_dir = fake_home / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "user-custom.md").write_text("don't touch me")
    installer.install_subagents()
    assert (agents_dir / "user-custom.md").read_text() == "don't touch me"
