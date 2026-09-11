from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["ArtifactRef"]


@dataclass(frozen=True)
class ArtifactRef:
    """A reference to an entity a run consumed or produced.

    A ref is either *local* or *external*:

    - **local** (``path`` set): makeprov reads size, media type, mtime and a
      SHA-256 digest off the filesystem via :meth:`resolve`.
    - **external** (``id`` set, no ``path``): makeprov records the IRI and
      whatever metadata the caller supplied, and never touches the filesystem.

    External refs are how a run cites a dataset, object-store key, model
    checkpoint or database snapshot by stable IRI without makeprov copying that
    resource's own metadata into the provenance document.

    Examples:
        .. code-block:: python

            ArtifactRef.local("results/model.pkl")
            ArtifactRef.external(
                "https://example.org/datasets/train-v17",
                types=("prov:Entity", "schema:Dataset"),
                digest="sha256:...",
            )
    """

    id: str | None = None
    path: Path | None = None
    types: tuple[str, ...] = ("prov:Entity",)
    digest: str | None = None
    media_type: str | None = None
    extent: int | None = None
    modified: str | None = None
    label: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if self.id is None and self.path is None:
            raise ValueError("ArtifactRef requires either an 'id' or a 'path'")

    @property
    def is_external(self) -> bool:
        return self.path is None

    @property
    def exists(self) -> bool:
        return self.path is not None and self.path.exists()

    @classmethod
    def local(
        cls,
        path: str | Path,
        *,
        types: tuple[str, ...] = ("prov:Entity",),
        label: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        """Reference a file on disk. Metadata is read lazily by :meth:`resolve`."""

        return cls(
            path=Path(path),
            types=tuple(types),
            label=label,
            extra=dict(extra or {}),
        )

    @classmethod
    def external(
        cls,
        id: str,
        *,
        types: tuple[str, ...] = ("prov:Entity",),
        digest: str | None = None,
        media_type: str | None = None,
        extent: int | None = None,
        label: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        """Reference an entity by IRI without touching the filesystem."""

        return cls(
            id=id,
            types=tuple(types),
            digest=digest,
            media_type=media_type,
            extent=extent,
            label=label,
            extra=dict(extra or {}),
        )

    def resolve(self) -> ArtifactRef:
        """Return a copy with filesystem metadata populated.

        External refs are returned unchanged. Local refs are stat-ed and hashed.

        Raises:
            FileNotFoundError: If this is a local ref and the path is missing.
                Callers decide the policy for that; this method does not guess.
        """

        if self.is_external:
            return self
        assert self.path is not None
        if not self.path.exists():
            raise FileNotFoundError(self.path)

        stat = self.path.stat()
        try:
            digest = f"sha256:{hashlib.sha256(self.path.read_bytes()).hexdigest()}"
        except OSError:
            digest = None

        return replace(
            self,
            digest=self.digest or digest,
            media_type=self.media_type
            or mimetypes.guess_type(self.path.name)[0]
            or "application/octet-stream",
            extent=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        )
