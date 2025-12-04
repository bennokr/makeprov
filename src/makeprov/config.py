from __future__ import annotations
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Literal, TYPE_CHECKING
import sys, logging, tomllib as toml, defopt
import argparse

if TYPE_CHECKING:
    from .core import Session

ProvFormat = Literal["json", "trig"]
Frame = Literal["provenance", "results"]

@dataclass
class ProvenanceConfig:
    """Runtime configuration for provenance generation.

    Args:
        base_iri: Default base IRI used when constructing provenance identifiers.
        prov_dir: Directory where provenance documents are written by default.
        prov_path: Explicit provenance output path that overrides ``prov_dir``.
        force: When ``True``, rebuild rules regardless of input/output freshness.
        merge: When ``True``, provenance from multiple rules is buffered and
            merged into a single document.
        dry_run: When ``True``, log rule execution without running the wrapped
            function.
        out_fmt: Output format for provenance files (``"json"`` or ``"trig"``).
        frame: Which structure to make primary subject of jsonld or 
            trig named graph. Options: `"provenance"` or `"results"`.
        context: Whether JSON-LD outputs include the context inline.

    """

    base_iri: str | None = None
    prov_dir: str = "prov"
    prov_path: str | None = None
    force: bool = False
    merge: bool = True
    dry_run: bool = False
    out_fmt: ProvFormat = "json"
    context: bool = False
    frame: Frame = "provenance"


_GLOBAL_CONFIG = ProvenanceConfig()


def get_config() -> ProvenanceConfig:
    """Return the process-wide provenance configuration instance."""

    return _GLOBAL_CONFIG


def set_config(config: ProvenanceConfig) -> ProvenanceConfig:
    """Replace the process-wide configuration values in place.

    The global instance is not rebound; instead, its fields are updated from the
    provided ``config`` object. This ensures existing imports continue to see
    the current configuration values without requiring callers to re-import a
    module attribute.

    Args:
        config: Configuration values to copy onto the global instance.

    Returns:
        ProvenanceConfig: The updated global configuration instance.
    """

    for f in fields(_GLOBAL_CONFIG):
        setattr(_GLOBAL_CONFIG, f.name, getattr(config, f.name))
    return _GLOBAL_CONFIG


def update_config(**kwargs) -> ProvenanceConfig:
    """Update selected configuration fields on the global instance.

    Args:
        **kwargs: Field names and values to apply. Unknown fields raise
            :class:`TypeError` via :func:`dataclasses.replace`.

    Returns:
        ProvenanceConfig: The updated global configuration instance.
    """

    new_config = replace(_GLOBAL_CONFIG, **kwargs)
    return set_config(new_config)


# Backwards compatibility: modules may still import GLOBAL_CONFIG directly. The
# object remains stable because ``set_config`` mutates it in place.
GLOBAL_CONFIG = _GLOBAL_CONFIG


def apply_config(conf_obj, toml_ref):
    """Update a dataclass configuration from TOML content.

    Args:
        conf_obj (dataclass): Configuration object to mutate in place.
        toml_ref (str): Either a TOML string or an ``@``-prefixed path to a
            TOML file.

    Raises:
        FileNotFoundError: If ``toml_ref`` points to a missing file.
        tomllib.TOMLDecodeError: If TOML content cannot be parsed.

    Examples:
        Load configuration overrides from a file and apply them to the global
        settings:

        .. code-block:: python

            from makeprov.config import GLOBAL_CONFIG, apply_config

            apply_config(GLOBAL_CONFIG, "@config/provenance.toml")
    """

    def set_conf(dc, params):
        for f in fields(dc):
            if f.name in params:
                cur, new = getattr(dc, f.name), params[f.name]
                if is_dataclass(cur) and isinstance(new, dict):
                    set_conf(cur, new)
                else:
                    setattr(dc, f.name, new)

    logging.debug(f"Parsing config {toml_ref}")
    t = toml_ref
    param = toml.load(open(t[1:], "rb")) if t.startswith("@") else toml.loads(t)
    logging.debug(f"Setting config {param}")
    set_conf(conf_obj, param)


def main(
    subcommands=None,
    conf_obj=None,
    argparse_kwargs={},
    *,
    session: "Session" | None = None,
    **kwargs,
):
    """Entry point for running registered CLI subcommands.

    Args:
        subcommands (Iterable[Callable] | None): Functions decorated with
            :func:`makeprov.core.rule` to expose on the command line; defaults to
            registered commands.
        conf_obj (ProvenanceConfig | None): Configuration to update from command
            line flags; defaults to :data:`GLOBAL_CONFIG`.
        session (Session | None): Registry and buffer container to use instead
            of the process-wide default session.

    Examples:
        Expose decorated rules as CLI commands and honor configuration flags:

        .. code-block:: bash

            python -m makeprov --conf @config/provenance.toml --verbose my_rule arg1
    """

    from .core import COMMANDS, Session as CoreSession, _get_session
    from .core import build, build_all, explain, flush_prov_buffer, start_prov_buffer, to_dot

    sess: CoreSession = _get_session(session)
    subcommands = subcommands or sess.commands
    conf_obj = conf_obj or get_config()

    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "-c",
        "--conf",
        action="append",
        default=[],
        help="Set config param from TOML snippet or @file.toml",
    )
    parent.add_argument(
        "-v", "--verbose", action="count", default=0, help="Show more logging output (-vv for even more)"
    )
    parent.add_argument(
        "--build-all", action="store_true",
        help="Build all concrete targets that have no dependents",
    )
    parent.add_argument(
        "--build",
        help="Recursively build a TARGET and its prerequisites",
        metavar="TARGET",
    )
    parent.add_argument(
        "--explain",
        help="Show dependency resolution for TARGET without running rules",
        metavar="TARGET",
    )
    parent.add_argument(
        "--to-dot",
        help="Render dependency graph for TARGET in DOT format",
        metavar="TARGET",
    )

    def apply_globals(argv):
        ns, _ = parent.parse_known_args(argv)
        lvl = ("WARNING", "INFO", "DEBUG")[min(max(ns.verbose, 0), 2)]
        logging.basicConfig(level=getattr(logging, lvl))
        for toml_ref in ns.conf:
            apply_config(conf_obj, toml_ref)
        return ns

    apply_globals(sys.argv[1:])  # apply effects early
    logging.debug(f"Config: {get_config()}")
    try:
        early_ns = parent.parse_known_args(sys.argv[1:])[0]
        if early_ns.build_all:
            build_all(session=sess)
            return
        if early_ns.build:
            build(early_ns.build, session=sess)
            return
        if early_ns.explain:
            explain(early_ns.explain, session=sess)
            return
        if early_ns.to_dot:
            print(to_dot(early_ns.to_dot, session=sess))
            return

        if conf_obj.merge:
            start_prov_buffer(session=sess)
        defopt.run(
            subcommands,
            argv=sys.argv[1:],
            argparse_kwargs={"parents": [parent], **argparse_kwargs},
            **kwargs
        )
    finally:
        if conf_obj.merge:
            flush_prov_buffer(session=sess)
