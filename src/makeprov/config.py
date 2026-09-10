from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Literal, TYPE_CHECKING, ClassVar
import sys, logging, tomllib as toml
import argparse

if TYPE_CHECKING:
    from .core import Session

ProvFormat = Literal["json", "trig"]
Frame = Literal["provenance", "results"]


class Config:
    """Base configuration container with TOML application helpers."""

    _global: ClassVar["Config"] | None = None

    @classmethod
    def get(cls) -> "Config":
        if cls._global is None:
            cls._global = cls()  # type: ignore[call-arg]
        return cls._global

    @classmethod
    def set(cls, config: "Config") -> "Config":
        cls._global = config
        return cls._global

    def clone_with(self, **kwargs) -> "Config":
        return replace(self, **kwargs)

    def apply(self, toml_ref: str) -> "Config":
        if not is_dataclass(self):
            raise TypeError("Configuration object must be a dataclass instance")

        def set_conf(dc, params):
            field_names = {f.name for f in fields(dc)}
            unknown = set(params) - field_names
            if unknown:
                unknown_list = ", ".join(sorted(unknown))
                raise KeyError(f"Unknown config fields: {unknown_list}")
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
        set_conf(self, param)
        return self


@dataclass
class ProvenanceConfig(Config):
    """Runtime configuration for provenance generation."""

    base_iri: str | None = None
    prov_dir: str = "prov"
    prov_path: str | None = None
    force: bool = False
    merge: bool = True
    dry_run: bool = False
    out_fmt: ProvFormat = "json"
    frame: Frame = "provenance"
    context: bool = False
    context_url: str = "https://w3id.org/makeprov/context"
    # When True (default), a failure to write provenance raises
    # ProvenanceWriteError instead of only logging a warning.
    strict: bool = True


# initialize global
ProvenanceConfig.set(ProvenanceConfig())


def main(
    subcommands=None,
    conf_obj=None,
    argparse_kwargs={},
    *,
    session: "Session" | None = None,
    **kwargs,
):
    """Entry point for running registered CLI subcommands.

    Requires the ``defopt`` package (install with ``pip install "makeprov[cli]"``),
    imported lazily here so that ``import makeprov`` does not require it.
    """

    try:
        import defopt
    except ImportError as exc:
        raise ImportError(
            "makeprov.main() requires the 'defopt' package. "
            'Install it with: pip install "makeprov[cli]"'
        ) from exc

    from .core import COMMANDS, Session as CoreSession, _get_session
    from .core import build, build_all, explain, flush_prov_buffer, start_prov_buffer, to_dot

    sess: CoreSession = _get_session(session)
    subcommands = subcommands or sess.commands
    conf_obj = conf_obj or ProvenanceConfig.get()

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
        "--build-all",
        action=argparse.BooleanOptionalAction,
        default=None,
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
        working_conf = replace(conf_obj)
        for toml_ref in ns.conf:
            working_conf.apply(toml_ref)
        if conf_obj is ProvenanceConfig.get():
            ProvenanceConfig.set(working_conf)
            updated_conf = ProvenanceConfig.get()
        else:
            updated_conf = working_conf
        return ns, updated_conf

    parsed_ns, conf_obj = apply_globals(sys.argv[1:])  # apply effects early
    logging.debug(f"Config: {conf_obj}")
    buffer_started = False
    try:
        early_ns = parsed_ns
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

        if conf_obj.merge and not sess.prov_buffers:
            start_prov_buffer(session=sess)
            buffer_started = True
        defopt.run(
            subcommands,
            argv=sys.argv[1:],
            argparse_kwargs={"parents": [parent], **argparse_kwargs},
            **kwargs
        )
    finally:
        if conf_obj.merge and buffer_started:
            flush_prov_buffer(session=sess)
