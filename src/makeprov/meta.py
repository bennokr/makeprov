"""Opt-in provenance attributes for rule arguments."""

from typing import Generic, TypeVar

T = TypeVar("T")


class ProvMeta(Generic[T]):
    """Annotate a rule argument to record its value, e.g. ``model: ProvMeta[str]``.

    The wrapped function receives the original value, not a ``ProvMeta`` object.
    Only JSON-serializable values are supported.
    """
