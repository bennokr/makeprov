from __future__ import annotations

import os
import sys
from pathlib import Path

import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Platform-appropriate base class for Path subclassing
_BasePath = type(Path())



def download_file(url: str, dest_path: str, *, chunk_size: int = 8192, headers: dict | None = None) -> None:
    """
    Download a file via HTTP GET using urllib, streaming to disk with progress logging.
    """
    logger = logging.getLogger(__name__)
    req = Request(url, headers=headers or {})

    try:
        with urlopen(req) as resp, open(dest_path, "wb") as out:
            total = resp.getheader("Content-Length")
            total = int(total) if total is not None else None

            downloaded = 0
            last_logged_pct = -1

            logger.debug("Starting download: url=%s dest=%s total_bytes=%s",
                         url, dest_path, total)

            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break

                out.write(chunk)
                downloaded += len(chunk)

                # Unknown total size: log every ~1MB
                if downloaded // (1024 * 1024) != (downloaded - len(chunk)) // (1024 * 1024):
                    logger.debug(
                        "Download progress: url=%s %d bytes",
                        url, downloaded
                    )

            logger.debug("Download complete: url=%s bytes=%d", url, downloaded)

    except HTTPError as e:
        logger.debug("HTTP error: url=%s code=%s", url, e.code)
        raise
    except URLError as e:
        logger.debug("Connection error: url=%s reason=%s", url, e.reason)
        raise

class ProvPath(_BasePath):
    """Filesystem path with first-class support for stream placeholders.

    Hyphen (``"-"``) paths are treated as stdin/stdout streams but still behave
    like :class:`pathlib.Path` instances for all other operations.

    Examples:
        .. code-block:: python

            from makeprov.paths import ProvPath

            p = ProvPath("-")
            assert p.is_stream
    """

    def __new__(cls, *paths: str | bytes | "ProvPath"):
        raw_paths = [os.fspath(p) for p in paths]
        self = super().__new__(cls, *paths)
        # We store stream flags on the instance. Path is immutable, but allows attributes.
        self._is_stream = len(raw_paths) == 1 and raw_paths[0] == "-"
        self._stream_name = None
        return self

    @property
    def is_stream(self) -> bool:
        return getattr(self, "_is_stream", False)

    @property
    def stream_name(self) -> str | None:
        return getattr(self, "_stream_name", None)

    def open(self, mode: str = "r", *args, **kwargs):
        """Open the path while respecting stream semantics.

        Args:
            mode (str): File open mode, passed through to ``Path.open`` when not
                operating on a stream.
            *args: Additional positional arguments forwarded to ``Path.open``.
            **kwargs: Additional keyword arguments forwarded to ``Path.open``.

        Returns:
            IOBase: A file-like object for the requested mode.

        Examples:
            .. code-block:: python

                from makeprov.paths import ProvPath

                with ProvPath("output.txt").open("w") as handle:
                    handle.write("hello")
        """
        if self.is_stream:
            if any(x in mode for x in ("w", "a", "+")):
                # Writing to stdout
                return sys.stdout.buffer if "b" in mode else sys.stdout
            # Reading from stdin
            return sys.stdin.buffer if "b" in mode else sys.stdin

        # Non-stream: ensure output dirs exist for write modes
        if any(x in mode for x in ("w", "a", "+")):
            self.parent.mkdir(parents=True, exist_ok=True)
        return super().open(mode, *args, **kwargs)


class InPath(ProvPath):
    """Marker for input paths where ``"-"`` maps to stdin.

    Examples:
        .. code-block:: python

            from makeprov.paths import InPath

            src = InPath("data/input.txt")
            with src.open() as handle:
                _ = handle.read()
    """

    def __new__(cls, *paths: str | bytes | ProvPath):
        self = super().__new__(cls, *paths)
        if self.is_stream:
            self._stream_name = "stdin"
        return self

    def open(self, mode: str = "r", *args, **kwargs):
        """Open the path for reading, honoring stdin streams.

        Args:
            mode (str): File mode; defaults to read.
            *args: Additional positional arguments forwarded to ``Path.open``.
            **kwargs: Additional keyword arguments forwarded to ``Path.open``.

        Returns:
            IOBase: Readable file-like object.

        Examples:
            .. code-block:: python

                InPath("example.txt").open().read()
        """
        if self.is_stream:
            return sys.stdin.buffer if "b" in mode else sys.stdin
        return super().open(mode, *args, **kwargs)


class CachedDownload(InPath):
    """Input wrapper that lazily downloads and records source metadata."""

    def __new__(cls, url: str, cache_path: str | os.PathLike[str], *, headers=None, transform: str | None = "prov:wasDerivedFrom"):
        self = super().__new__(cls, cache_path)
        return self

    def __init__(
        self,
        url: str,
        cache_path: str | os.PathLike[str],
        *,
        headers: dict | None = None,
        transform: str | None = "prov:wasDerivedFrom",
    ):
        super().__init__()
        self.url = url
        self.headers = headers or {}
        self.transform = transform or "prov:wasDerivedFrom"

    def open(self, mode: str = "r", *args, **kwargs):
        if any(x in mode for x in ("w", "a", "+")):
            return super().open(mode, *args, **kwargs)
        if not self.exists():
            download_file(self.url, str(self), headers=self.headers or None)
        return super().open(mode, *args, **kwargs)

class OutPath(ProvPath):
    """Marker for output paths where ``"-"`` maps to stdout.

    Examples:
        .. code-block:: python

            from makeprov.paths import OutPath

            dest = OutPath("data/output.txt")
            dest.write_text("generated")
    """

    def __new__(cls, *paths: str | bytes | ProvPath):
        self = super().__new__(cls, *paths)
        if self.is_stream:
            self._stream_name = "stdout"
        return self

    def as_inpath(self) -> InPath:
        """Convert an output marker into an input marker.

        Returns:
            InPath: A new instance pointing to the same filesystem location.

        Raises:
            ValueError: If the current path represents a stream.

        Examples:
            .. code-block:: python

                from makeprov.paths import OutPath

                OutPath("data/output.txt").as_inpath()
        """
        if self.is_stream:
            raise ValueError("Cannot convert stream-based OutPath '-' into InPath")
        return InPath(str(self))

    def open(self, mode: str = "w", *args, **kwargs):
        """Open the path for writing, creating parent directories when needed.

        Args:
            mode (str): File mode; defaults to write.
            *args: Additional positional arguments forwarded to ``Path.open``.
            **kwargs: Additional keyword arguments forwarded to ``Path.open``.

        Returns:
            IOBase: Writable file-like object.

        Examples:
            .. code-block:: python

                with OutPath("output.txt").open("w") as handle:
                    handle.write("hello")
        """
        if self.is_stream:
            return sys.stdout.buffer if "b" in mode else sys.stdout
        # Ensure directories exist for output
        self.parent.mkdir(parents=True, exist_ok=True)
        return super().open(mode, *args, **kwargs)


class OutDir(OutPath):
    """Output directory that tracks files declared within it.

    The :meth:`file` helper produces :class:`OutPath` instances rooted in the
    directory while recording them for provenance collection.
    """

    def __new__(cls, *paths: str | bytes | ProvPath):  # type: ignore[override]
        self = super().__new__(cls, *paths)
        self._children: list[OutPath] = []
        return self

    def file(self, name: str | os.PathLike[str]) -> OutPath:
        child = OutPath(self / name)
        self._children.append(child)
        return child

    @property
    def children(self) -> tuple[OutPath, ...]:
        return tuple(self._children)


class InDir(InPath):
    """Input directory that tracks files declared within it.

    The :meth:`file` helper produces :class:`InPath` instances rooted in the
    directory while recording them for provenance collection.
    """

    def __new__(cls, *paths: str | bytes | ProvPath):  # type: ignore[override]
        self = super().__new__(cls, *paths)
        self._children: list[InPath] = []
        return self

    def file(self, name: str | os.PathLike[str]) -> InPath:
        child = InPath(self / name)
        self._children.append(child)
        return child

    @property
    def children(self) -> tuple[InPath, ...]:
        return tuple(self._children)
