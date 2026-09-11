"""Tests for declarative forge profiles and remote normalization."""

from __future__ import annotations

import pytest

from makeprov.forges import (
    ForgeProfile,
    load_forges,
    normalize_remote,
    resolve_forge,
)


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("https://github.com/o/r.git", "https://github.com/o/r"),
        ("git@github.com:o/r.git", "https://github.com/o/r"),
        ("ssh://git@gitlab.com/o/r.git", "https://gitlab.com/o/r"),
        ("https://gitlab.com/group/sub/r", "https://gitlab.com/group/sub/r"),
        ("https://github.com/o/r/", "https://github.com/o/r"),
    ],
)
def test_normalize_remote_forms(origin, expected):
    assert normalize_remote(origin)[1] == expected


def test_normalize_remote_strips_credentials():
    """A remote may embed a token; it must never reach a provenance document."""

    host, repo = normalize_remote("https://user:ghp_secret@github.com/o/r.git")

    assert host == "github.com"
    assert repo == "https://github.com/o/r"
    assert "ghp_secret" not in repo
    assert "user" not in repo


def test_normalize_remote_keeps_dot_git_inside_name():
    """removesuffix, not replace: only a trailing .git is an extension."""

    assert normalize_remote("https://github.com/o/r.github.io")[1] == (
        "https://github.com/o/r.github.io"
    )


@pytest.mark.parametrize(
    "origin,forge,suffix",
    [
        ("https://github.com/o/r", "github", "/blob/SHA/"),
        ("https://gitlab.com/o/r", "gitlab", "/-/blob/SHA/"),
        ("https://bitbucket.org/o/r", "bitbucket", "/src/SHA/"),
        ("https://codeberg.org/o/r", "forgejo", "/src/commit/SHA/"),
        ("https://git.sr.ht/~u/r", "sourcehut", "/tree/SHA/item/"),
    ],
)
def test_builtin_forge_blob_layouts(origin, forge, suffix):
    profile, repo = resolve_forge(origin)

    assert profile.name == forge
    assert profile.blob_prefix(repo, "SHA") == f"{repo}{suffix}"


def test_unknown_host_does_not_match():
    assert resolve_forge("https://svn.example.org/o/r") is None
    assert resolve_forge("") is None
    assert resolve_forge(None) is None


def test_user_profiles_take_precedence(tmp_path):
    """Self-hosted instances need to add hosts and override built-in ones."""

    extra = tmp_path / "forges.toml"
    extra.write_text(
        """
[[forge]]
name = "corp"
hosts = ["git.corp.example", "github.com"]
blob = "{repo}/raw/{revision}/"
""",
        encoding="utf-8",
    )

    profiles = load_forges(str(extra))

    self_hosted, repo = resolve_forge("git@git.corp.example:team/r.git", profiles)
    assert self_hosted.name == "corp"
    assert repo == "https://git.corp.example/team/r"

    # The user entry is matched before the built-in github profile.
    overridden, _ = resolve_forge("https://github.com/o/r", profiles)
    assert overridden.name == "corp"


def test_builtin_profiles_still_apply_alongside_user_profiles(tmp_path):
    extra = tmp_path / "forges.toml"
    extra.write_text(
        '[[forge]]\nname = "corp"\nhosts = ["git.corp.example"]\nblob = "{repo}/x/{revision}/"\n',
        encoding="utf-8",
    )

    profile, _ = resolve_forge("https://gitlab.com/o/r", load_forges(str(extra)))
    assert profile.name == "gitlab"


def test_profile_is_hashable_for_caching():
    profile = ForgeProfile("x", ("h",), "{repo}/{revision}/")
    assert hash(profile)
