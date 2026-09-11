from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

__all__ = ["ForgeProfile", "load_forges", "normalize_remote", "resolve_forge"]

_BUILTIN = Path(__file__).parent / "forges.toml"

# scp-style remote, e.g. git@github.com:owner/repo.git
_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")


@dataclass(frozen=True)
class ForgeProfile:
    """How one git host builds a permalink to a file at a revision."""

    name: str
    hosts: tuple[str, ...]
    blob: str

    def blob_prefix(self, repo: str, revision: str) -> str:
        return self.blob.format(repo=repo, revision=revision)


def _parse(path: Path) -> list[ForgeProfile]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        ForgeProfile(
            name=entry.get("name", "unnamed"),
            hosts=tuple(h.lower() for h in entry.get("hosts", [])),
            blob=entry["blob"],
        )
        for entry in data.get("forge", [])
    ]


@lru_cache(maxsize=8)
def load_forges(extra: str | None = None) -> tuple[ForgeProfile, ...]:
    """Load the built-in profiles, preceded by any user-supplied ones.

    User profiles are matched first so they can override a built-in host, which
    is what self-hosted instances of a known forge need.
    """

    profiles = _parse(Path(extra)) if extra else []
    return tuple(profiles) + tuple(_parse(_BUILTIN))


def normalize_remote(origin: str) -> tuple[str, str] | None:
    """Return ``(host, repo_url)`` for a git remote, or ``None`` if unparseable.

    Handles both URL and scp-style remotes, and drops any userinfo: a remote
    like ``https://user:token@host/o/r.git`` must never put its credentials into
    a provenance document.
    """

    origin = (origin or "").strip()
    if not origin:
        return None

    if "://" in origin:
        parts = urlsplit(origin)
        host, path = parts.hostname, parts.path
    else:
        match = _SCP.match(origin)
        if not match:
            return None
        host, path = match.group("host").lower(), "/" + match.group("path")

    if not host or not path:
        return None

    # removesuffix, not replace: a repo may legitimately contain ".git" inside
    # its name.
    path = path.removesuffix("/").removesuffix(".git")
    if not path.startswith("/"):
        path = "/" + path
    return host, f"https://{host}{path}"


def resolve_forge(
    origin: str | None,
    profiles: tuple[ForgeProfile, ...] | None = None,
) -> tuple[ForgeProfile, str] | None:
    """Match a git remote against the known forges.

    Returns the matching profile and the normalized repository URL, or ``None``
    when the remote is unparseable or its host is not a known forge.
    """

    if not origin:
        return None
    normalized = normalize_remote(origin)
    if not normalized:
        return None

    host, repo = normalized
    for profile in profiles if profiles is not None else load_forges():
        if host in profile.hosts:
            return profile, repo
    return None
