from __future__ import annotations

import os
import sys
from pathlib import Path

# Platform-appropriate base class for Path subclassing
_BasePath = type(Path())


class ProvPath(_BasePath):
    """
    A Path subclass that understands '-' as a special stream path.
    For subclasses InPath and OutPath, '-' maps to stdin/stdout, respectively.
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
        """
        Default behavior: respects '-' as stdin for read modes and stdout for write modes.
        Subclasses override to enforce direction.
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
    """Marker for input paths. '-' means stdin."""
    def __new__(cls, *paths: str | bytes | ProvPath):
        self = super().__new__(cls, *paths)
        if self.is_stream:
            self._stream_name = "stdin"
        return self

    def open(self, mode: str = "r", *args, **kwargs):
        if self.is_stream:
            return sys.stdin.buffer if "b" in mode else sys.stdin
        return super().open(mode, *args, **kwargs)


class OutPath(ProvPath):
    """Marker for output paths. '-' means stdout."""
    def __new__(cls, *paths: str | bytes | ProvPath):
        self = super().__new__(cls, *paths)
        if self.is_stream:
            self._stream_name = "stdout"
        return self

    def as_inpath(self) -> InPath:
        """Convert an OutPath to an InPath (disallowed for streams)."""
        if self.is_stream:
            raise ValueError("Cannot convert stream-based OutPath '-' into InPath")
        return InPath(str(self))

    def open(self, mode: str = "w", *args, **kwargs):
        if self.is_stream:
            return sys.stdout.buffer if "b" in mode else sys.stdout
        # Ensure directories exist for output
        self.parent.mkdir(parents=True, exist_ok=True)
        return super().open(mode, *args, **kwargs)