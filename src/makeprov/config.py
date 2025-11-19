from __future__ import annotations
from dataclasses import dataclass, fields, is_dataclass
from typing import Literal
import sys, logging, tomllib as toml, defopt
import argparse

ProvFormat = Literal["json", "trig"]

@dataclass
class ProvenanceConfig:
    base_iri: str = "http://example.org/"
    prov_dir: str = "prov"
    prov_path: str | None = None
    force: bool = False
    dry_run: bool = False
    out_fmt: ProvFormat = "json"
    jsonld_with_context: bool = False

GLOBAL_CONFIG = ProvenanceConfig()

def main(subcommands=None, conf_obj=None, parsers=None):
    from .core import COMMANDS, flush_prov_buffer, start_prov_buffer
    global GLOBAL_CONFIG

    subcommands = subcommands or COMMANDS
    conf_obj = conf_obj or GLOBAL_CONFIG

    def conf(dc, params):
        for f in fields(dc):
            if f.name in params:
                cur, new = getattr(dc, f.name), params[f.name]
                if is_dataclass(cur) and isinstance(new, dict):
                    conf(cur, new)
                else:
                    setattr(dc, f.name, new)

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
        for t in ns.conf:
            logging.debug(f"Parsing config {t}")
            p = toml.load(open(t[1:], "rb")) if t.startswith("@") else toml.loads(t)
            logging.debug(f"Setting config {p}")
            conf(conf_obj, p)

        return ns

    parent.add_argument(
        "--merge-prov",
        action="store_true",
        help="Merge provenance from invoked commands into a single output",
    )

    ns = apply_globals(sys.argv[1:])  # apply effects early
    logging.debug(f"Config: {GLOBAL_CONFIG}")
    try:
        if ns.merge_prov:
            start_prov_buffer()
        defopt.run(
            subcommands,
            parsers=parsers or {},
            argv=sys.argv[1:],
            argparse_kwargs={"parents": [parent]},
        )
    finally:
        if ns.merge_prov:
            flush_prov_buffer()
