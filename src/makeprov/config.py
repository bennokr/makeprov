from __future__ import annotations
from dataclasses import dataclass, fields, is_dataclass
from typing import Literal
import sys, logging, tomllib as toml, defopt
import argparse

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

    Examples:
        .. code-block:: python

            from makeprov import ProvenanceConfig, GLOBAL_CONFIG

            GLOBAL_CONFIG = ProvenanceConfig(
                prov_dir="artifacts/prov", out_fmt="trig"
            )
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


GLOBAL_CONFIG = ProvenanceConfig()


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


def main(subcommands=None, conf_obj=None, argparse_kwargs={}, **kwargs):
    """Entry point for running registered CLI subcommands.

    Args:
        subcommands (Iterable[Callable] | None): Functions decorated with
            :func:`makeprov.core.rule` to expose on the command line; defaults to
            registered commands.
        conf_obj (ProvenanceConfig | None): Configuration to update from command
            line flags; defaults to :data:`GLOBAL_CONFIG`.

    Examples:
        Expose decorated rules as CLI commands and honor configuration flags:

        .. code-block:: bash

            python -m makeprov --conf @config/provenance.toml --verbose my_rule arg1
    """

    from .core import COMMANDS, flush_prov_buffer, start_prov_buffer
    from .core import build, build_all, explain, to_dot

    global GLOBAL_CONFIG

    subcommands = subcommands or COMMANDS
    conf_obj = conf_obj or GLOBAL_CONFIG

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
    logging.debug(f"Config: {GLOBAL_CONFIG}")
    try:
        early_ns = parent.parse_known_args(sys.argv[1:])[0]
        if early_ns.build_all:
            build_all()
            return
        if early_ns.build:
            build(early_ns.build)
            return
        if early_ns.explain:
            explain(early_ns.explain)
            return
        if early_ns.to_dot:
            print(to_dot(early_ns.to_dot))
            return

        if GLOBAL_CONFIG.merge:
            start_prov_buffer()
        defopt.run(
            subcommands,
            argv=sys.argv[1:],
            argparse_kwargs={"parents": [parent], **argparse_kwargs},
            **kwargs
        )
    finally:
        if GLOBAL_CONFIG.merge:
            flush_prov_buffer()
