from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from makeprov import prov as prov_mod
from makeprov.prov import IriMinter, resolve_iris


def _pinned(repo_root="/repo", file_segment=""):
    return IriMinter(
        "https://github.com/example/repo",
        "",
        pinned=True,
        file_segment=file_segment,
        repo_root=Path(repo_root),
    )


def test_minter_pins_repo_relative_paths():
    assert _pinned().file("data/a.txt") == "blob:data/a.txt"


def test_minter_rewrites_absolute_paths_inside_repo():
    """An absolute path inside the checkout is still a repo-relative blob."""

    assert _pinned().file("/repo/data/a.txt") == "blob:data/a.txt"


def test_minter_uses_file_uri_outside_repo():
    """`blob:` expands to <repo>/blob/<commit>/<path>, so it must not be used
    for paths that are not in the repository at all."""

    assert _pinned().file("/tmp/scratch/a.txt") == "file:///tmp/scratch/a.txt"


def test_minter_without_repo_root_does_not_fabricate_blob_iris():
    minter = IriMinter("https://github.com/example/repo", "", pinned=True)
    assert minter.file("/tmp/a.txt") == "file:///tmp/a.txt"


def test_minter_relative_mode_keeps_file_segment():
    minter = IriMinter(None, "", pinned=False, file_segment="file/")
    assert minter.file("data/a.txt") == "file/data/a.txt"


def test_resolve_iris_prefers_explicit_base(monkeypatch):
    def boom(argv):  # pragma: no cover - must not be reached
        raise AssertionError("git should not be consulted when a base is given")

    monkeypatch.setattr(prov_mod, "_safe_cmd", boom)
    context: dict = {}
    minter = resolve_iris("https://example.org/wf", context)

    assert minter.mint("rule/x") == "https://example.org/wf/rule/x"
    assert "@base" not in context


def test_prov_context_is_isolated_from_common(monkeypatch):
    before = deepcopy(prov_mod.COMMON_CONTEXT)

    responses = {
        "git rev-parse HEAD": "abc123",
        "git config --get remote.origin.url": "https://github.com/example/repo.git",
        "git rev-parse --abbrev-ref HEAD": "main",
    }

    def fake_safe_cmd(argv: list[str]):
        return responses.get(" ".join(argv))

    monkeypatch.setattr(prov_mod, "_safe_cmd", fake_safe_cmd)

    t0 = datetime.now(timezone.utc)
    prov = prov_mod.Prov.create(
        base_iri=None,
        name="context",
        run_id="run-1",
        t0=t0,
        t1=t0,
        inputs=[],
        outputs=[],
        results=[],
        success=True,
    )

    assert prov_mod.COMMON_CONTEXT == before
    assert prov.context is not prov_mod.COMMON_CONTEXT
    assert prov.context.get("@base") == "https://github.com/example/repo#"
    # Pinned to the commit, not the branch: a branch URL names different bytes
    # after every push, so the same IRI would denote different content.
    assert prov.context.get("blob") == "https://github.com/example/repo/blob/abc123/"


def test_prov_context_falls_back_to_branch_without_commit(monkeypatch):
    responses = {
        "git config --get remote.origin.url": "https://github.com/example/repo.git",
        "git rev-parse --abbrev-ref HEAD": "main",
    }

    monkeypatch.setattr(
        prov_mod, "_safe_cmd", lambda argv: responses.get(" ".join(argv))
    )

    t0 = datetime.now(timezone.utc)
    prov = prov_mod.Prov.create(
        base_iri=None,
        name="context",
        run_id="run-1",
        t0=t0,
        t1=t0,
        inputs=[],
        outputs=[],
        results=[],
        success=True,
    )

    assert prov.context.get("blob") == "https://github.com/example/repo/blob/main/"
