from __future__ import annotations
from dataclasses import dataclass, fields, is_dataclass
from typing import Literal
import sys, logging, tomllib as toml, defopt
import argparse

ProvFormat = Literal["json", "trig"]


@dataclass
class ProvenanceConfig:
    base_iri: str | None = None
    prov_dir: str = "prov"
    prov_path: str | None = None
    force: bool = False
    merge: bool = True
    dry_run: bool = False
    out_fmt: ProvFormat = "json"
    context: bool = False


GLOBAL_CONFIG = ProvenanceConfig()


def apply_config(conf_obj, toml_ref):
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


def main(subcommands=None, conf_obj=None, parsers=None):
    from .core import COMMANDS, flush_prov_buffer, start_prov_buffer

    global GLOBAL_CONFIG

    subcommands = subcommands or COMMANDS
    conf_obj = conf_obj or GLOBAL_CONFIG

    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "-c",
        "--conf",
        action="append",
        default=[],
        help="Set config param from TOML snippet or @file",
    )
    parent.add_argument(
        "-v", "--verbose", action="count", default=0, help="Show more logging output"
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
        if GLOBAL_CONFIG.merge:
            start_prov_buffer()
        defopt.run(
            subcommands,
            parsers=parsers or {},
            argv=sys.argv[1:],
            argparse_kwargs={"parents": [parent]},
        )
    finally:
        if GLOBAL_CONFIG.merge:
            flush_prov_buffer()
