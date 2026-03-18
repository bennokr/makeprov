from __future__ import annotations

from contextlib import ContextDecorator
from pathlib import Path
from typing import Any

from .config import ProvenanceConfig
from .core import flush_prov_buffer, start_prov_buffer


class span(ContextDecorator):
    """Scope provenance buffering to a context or decorator.

    Starting a span begins a provenance buffer; exiting flushes it to disk or
    merges it into the parent buffer. This avoids manual buffer start/flush
    orchestration around backend calls.
    """

    def __init__(
        self,
        label: str,
        prov_path: str | Path | None = None,
        *,
        frame: str | None = None,
        context: bool | None = None,
        session: Any | None = None,
    ) -> None:
        self.label = label
        self.prov_path = prov_path
        self.frame = frame
        self.context = context
        self.session = session

    def __enter__(self):
        start_prov_buffer(session=self.session)
        return self

    def __exit__(self, exc_type, exc, tb):
        cfg = ProvenanceConfig.get()
        destination = self.prov_path or Path(cfg.prov_dir) / self.label
        flush_prov_buffer(
            prov_path=destination,
            frame=self.frame,
            context=self.context,
            session=self.session,
        )
        # Propagate exceptions
        return False
