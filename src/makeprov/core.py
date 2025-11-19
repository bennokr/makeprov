from __future__ import annotations

import functools
import inspect
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from collections.abc import Callable

from .config import ProvenanceConfig, ProvFormat, GLOBAL_CONFIG
from .paths import InPath, OutPath
from .prov import Prov, ProvResult, write_combined_prov

try:
    import rdflib  # optional
except Exception:
    rdflib = None

# Simple Make-like registry
RULES: dict[str, dict[str, Any]] = {}
COMMANDS: set[Callable] = set()


PROV_BUFFER: list[ProvResult] | None = None


def start_prov_buffer() -> None:
    global PROV_BUFFER
    PROV_BUFFER = []


def flush_prov_buffer() -> None:
    global PROV_BUFFER
    try:
        if PROV_BUFFER:
            write_combined_prov(
                PROV_BUFFER,
                prov_path=GLOBAL_CONFIG.prov_path or Path(GLOBAL_CONFIG.prov_dir)
                / "combined",
                fmt=GLOBAL_CONFIG.out_fmt,
                jsonld_with_context=GLOBAL_CONFIG.jsonld_with_context,
            )
    finally:
        PROV_BUFFER = None

def needs_update(outputs, deps) -> bool:
    """Return True if any output missing or older than any dependency."""
    out_paths = [Path(o) for o in outputs]
    dep_paths = [Path(d) for d in deps]

    if not out_paths:
        return True
    if any(not o.exists() for o in out_paths):
        return True

    oldest_out = min(o.stat().st_mtime for o in out_paths)
    dep_times = [d.stat().st_mtime for d in dep_paths if d.exists()]
    if not dep_times:
        return False
    newest_dep = max(dep_times)
    return newest_dep > oldest_out

def build(target, _seen=None):
    """
    Recursively build target after its dependencies, if needed.
    `target` is a path (string/Path). Only rules with default OutPath are in DAG.
    """
    top_level = _seen is None
    if _seen is None:
        _seen = set()
    target = str(target)
    if target in _seen:
        raise RuntimeError(f"Cycle in build graph at {target!r}")
    _seen.add(target)

    if top_level:
        start_prov_buffer()

    rule = RULES[target]
    for dep in rule["deps"]:
        if dep in RULES:
            build(dep, _seen)
    rule["func"]()

    if top_level:
        flush_prov_buffer()

def _is_kind_annotation(ann: Any, cls: type) -> bool:
    if ann is cls:
        return True
    origin = get_origin(ann)
    if origin is None:
        return False
    return any(a is cls for a in get_args(ann))

def rule(
    *,
    name: str | None = None,
    base_iri: str | None = None,
    prov_dir: str | None = None,
    prov_path: str | None = None,
    force: bool | None = None,
    dry_run: bool | None = None,
    out_fmt: ProvFormat | None = None,
    config: ProvenanceConfig | None = None,
    jsonld_with_context: bool | None = None,
):
    """
    Decorator that infers inputs/outputs from type annotations
    (InPath / OutPath, including Optional[...] unions) and writes provenance.
    """

    def decorator(func):

        sig = inspect.signature(func)
        hints = get_type_hints(func)

        in_params: list[str] = []
        out_params: list[str] = []
        for p in sig.parameters.values():
            ann = hints.get(p.name, p.annotation)
            if _is_kind_annotation(ann, InPath):
                in_params.append(p.name)
            if _is_kind_annotation(ann, OutPath):
                out_params.append(p.name)

        if not out_params:
            raise ValueError(
                f"Function {func.__name__} must have at least one OutPath "
                f"(possibly Optional[OutPath]) parameter"
            )

        deps: list[str] = []
        outputs: list[str] = []
        for p in sig.parameters.values():
            if p.name in in_params and p.default is not inspect._empty:
                val = p.default
                if isinstance(val, InPath):
                    if not val.is_stream:
                        deps.append(str(val))
                elif isinstance(val, (str, Path)):
                    if str(val) != "-":
                        deps.append(str(val))
            if p.name in out_params and p.default is not inspect._empty:
                val = p.default
                if isinstance(val, OutPath):
                    if not val.is_stream:
                        outputs.append(str(val))
                elif isinstance(val, (str, Path)):
                    if str(val) != "-":
                        outputs.append(str(val))

        register_for_build = bool(outputs)
        logical_name = name or func.__name__

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()

            global GLOBAL_CONFIG
            base_config = config or GLOBAL_CONFIG
            rule_config = ProvenanceConfig(
                base_iri=base_iri if base_iri is not None else base_config.base_iri,
                prov_dir=prov_dir if prov_dir is not None else base_config.prov_dir,
                prov_path=base_config.prov_path,
                force=force if force is not None else base_config.force,
                dry_run=dry_run if dry_run is not None else base_config.dry_run,
                out_fmt=out_fmt if out_fmt is not None else base_config.out_fmt,
                jsonld_with_context=base_config.jsonld_with_context,
            )

            effective_jsonld_with_context = (
                jsonld_with_context
                if jsonld_with_context is not None
                else rule_config.jsonld_with_context
            )

            in_files: list[Path] = []
            out_files: list[Path] = []

            for pname in in_params:
                val = bound.arguments.get(pname)
                if isinstance(val, InPath):
                    if not val.is_stream:
                        in_files.append(Path(val))
                elif val is None:
                    continue
                else:
                    if str(val) != "-":
                        in_files.append(Path(val))

            for pname in out_params:
                val = bound.arguments.get(pname)
                if isinstance(val, OutPath):
                    if not val.is_stream:
                        out_files.append(Path(val))
                elif val is None:
                    continue
                else:
                    if str(val) != "-":
                        out_files.append(Path(val))

            if not rule_config.force and not needs_update(out_files, in_files):
                logging.info("Skipping %s (up to date)", logical_name)
                return None

            if rule_config.dry_run:
                logging.info(
                    "Dry-run %s: would run with %s -> %s",
                    logical_name,
                    in_files,
                    out_files,
                )
                return None

            t0 = datetime.now(timezone.utc)
            exc: Exception | None = None
            result = None

            try:
                result = func(*bound.args, **bound.kwargs)
                return result
            except Exception as e:
                exc = e
                raise
            finally:
                t1 = datetime.now(timezone.utc)
                try:
                    prov = Prov(
                        base_iri=rule_config.base_iri,
                        name=logical_name,
                        run_id=t0.strftime("%Y%m%dT%H%M%S"),
                        t0=t0,
                        t1=t1,
                        inputs=[Path(p) for p in in_files],
                        outputs=[Path(p) for p in out_files],
                        success=exc is None,
                    )
                    if prov_path is not None:
                        rule_prov_path = prov_path
                    elif rule_config.prov_path is not None:
                        rule_prov_path = rule_config.prov_path
                    else:
                        rule_prov_path = Path(rule_config.prov_dir) / logical_name

                    if PROV_BUFFER is not None:
                        PROV_BUFFER.append(ProvResult(prov, result))
                    else:
                        prov.write(
                            rule_prov_path,
                            fmt=rule_config.out_fmt,
                            result=result,
                            jsonld_with_context=effective_jsonld_with_context,
                        )
                except Exception as prov_exc:  # noqa: BLE001
                    logging.warning("Failed to write provenance for %s: %s", logical_name, prov_exc)

        COMMANDS.add(wrapped)
        if register_for_build:
            target = outputs[0]
            RULES[target] = {
                "deps": deps,
                "outputs": outputs,
                "func": wrapped,
            }

        return wrapped

    return decorator
