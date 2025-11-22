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
from .prov import Prov
from .rdfmixin import RDFMixin

try:
    import rdflib  # optional
except Exception:
    rdflib = None

# Simple Make-like registry
RULES: dict[str, dict[str, Any]] = {}
COMMANDS: set[Callable] = set()
PROV_BUFFER: list[Prov] | None = None


def start_prov_buffer() -> None:
    """Create a provenance buffer to batch writes.

    This function is typically invoked at the start of a command-line session
    (see :func:`makeprov.config.main`) to accumulate ``Prov`` objects produced
    by multiple rule executions. Buffered provenance is merged and flushed with
    :func:`flush_prov_buffer`.

    Examples:
        Start buffering provenance records before running several rules:

        ```python
        from makeprov.core import start_prov_buffer, flush_prov_buffer

        start_prov_buffer()
        try:
            rule_a()
            rule_b()
        finally:
            flush_prov_buffer()
        ```
    """

    global PROV_BUFFER
    PROV_BUFFER = []


def flush_prov_buffer() -> None:
    """Write and clear any buffered provenance records.

    If :func:`start_prov_buffer` has been called and the buffer contains
    ``Prov`` objects, they are merged into a single document and written using
    the global configuration. The buffer is cleared regardless of write
    success, so callers should wrap their work in a ``try/finally`` block.

    Examples:
        Flush the buffer after running a series of build steps:

        ```python
        from makeprov.core import flush_prov_buffer, start_prov_buffer

        start_prov_buffer()
        # ... run decorated rules ...
        flush_prov_buffer()
        ```
    """

    global PROV_BUFFER
    try:
        if PROV_BUFFER:
            prov = Prov.merge(PROV_BUFFER)
            prov.write(
                prov_path=GLOBAL_CONFIG.prov_path
                or Path(GLOBAL_CONFIG.prov_dir) / prov.name,
                fmt=GLOBAL_CONFIG.out_fmt,
                context=GLOBAL_CONFIG.context,
            )
    finally:
        PROV_BUFFER = None


def needs_update(outputs, deps) -> bool:
    """Determine whether outputs are stale relative to dependencies.

    Args:
        outputs (Iterable[str | Path]): Output files expected to exist after a
            rule runs.
        deps (Iterable[str | Path]): Dependency files that must be newer than
            outputs for a rebuild to be unnecessary.

    Returns:
        bool: ``True`` if any output is missing or older than a dependency; the
        absence of dependencies returns ``False`` to avoid unnecessary rebuilds.

    Examples:
        ```python
        from makeprov.core import needs_update

        if needs_update(["data/output.txt"], ["data/input.txt"]):
            regenerate()
        ```
    """
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


def _is_kind_annotation(ann: Any, cls: type) -> bool:
    """Check whether a type annotation represents a specific path marker.

    Args:
        ann (Any): The annotation retrieved from a function parameter.
        cls (type): The marker class to detect, such as :class:`InPath` or
            :class:`OutPath`.

    Returns:
        bool: ``True`` if the annotation directly references ``cls`` or a
        union/optional type containing it.

    Examples:
        ```python
        from typing import Optional
        from makeprov.core import _is_kind_annotation
        from makeprov.paths import InPath

        _is_kind_annotation(Optional[InPath], InPath)  # True
        ```
    """

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
    context: bool | None = None,
):
    """Decorate a function as a build rule with automatic provenance.

    Args:
        name (str | None): Logical name for the rule; defaults to the function
            name.
        base_iri (str | None): Base IRI for provenance identifiers; overrides
            global configuration when provided.
        prov_dir (str | None): Directory where provenance documents are saved.
        prov_path (str | None): Explicit path for the provenance file; overrides
            ``prov_dir`` when set.
        force (bool | None): When ``True``, always run the rule regardless of
            timestamps.
        dry_run (bool | None): When ``True``, log activity without executing the
            wrapped function.
        out_fmt (ProvFormat | None): Output format for provenance files
            (``"json"`` or ``"trig"``).
        config (ProvenanceConfig | None): Configuration object to use instead of
            :data:`makeprov.config.GLOBAL_CONFIG`.
        context (bool | None): Whether to embed JSON-LD context in output when
            writing provenance.

    Returns:
        Callable: A decorator that wraps the target function and registers it as
        a rule when outputs are discoverable from annotations.

    Examples:
        Annotate parameters with :class:`InPath` and :class:`OutPath` to let the
        decorator infer dependencies:

        ```python
        from makeprov import InPath, OutPath, rule

        @rule()
        def uppercase(src: InPath, dst: OutPath):
            dst.write_text(src.read_text().upper())

        uppercase("data/input.txt", "data/output.txt")
        ```
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
                context=base_config.context,
            )

            effective_context = context if context is not None else rule_config.context

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
                    if isinstance(result, RDFMixin):
                        results = [result]
                    else:
                        results = []
                        if isinstance(result, (list, tuple, set)):
                            for r in result:
                                if isinstance(r, RDFMixin):
                                    results.append(r)

                    prov = Prov.create(
                        base_iri=rule_config.base_iri,
                        name=logical_name,
                        run_id=t0.strftime("%Y%m%dT%H%M%S"),
                        t0=t0,
                        t1=t1,
                        inputs=[Path(p) for p in in_files],
                        outputs=[Path(p) for p in out_files],
                        results=results,
                        success=exc is None,
                    )
                    if prov_path is not None:
                        rule_prov_path = prov_path
                    elif rule_config.prov_path is not None:
                        rule_prov_path = rule_config.prov_path
                    else:
                        rule_prov_path = Path(rule_config.prov_dir) / logical_name

                    if PROV_BUFFER is not None:
                        PROV_BUFFER.append(prov)
                    else:
                        prov.write(
                            rule_prov_path,
                            fmt=rule_config.out_fmt,
                            context=effective_context,
                        )
                except Exception as prov_exc:  # noqa: BLE001
                    logging.warning(
                        "Failed to write provenance for %s: %s", logical_name, prov_exc
                    )

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


@rule()
def build(target: OutPath, _seen=None):
    """Recursively build a target after its dependencies when stale.

    Args:
        target (OutPath): Path to the output to build. The path must correspond
            to a rule that was registered via :func:`rule`.
        _seen (set[str] | None): Internal set to detect graph cycles; callers
            typically omit this argument.

    Returns:
        None: The function executes registered rule callbacks for dependencies
        and the target in topological order.

    Examples:
        ```python
        from makeprov import build

        # Build a specific target created by a decorated rule
        build("data/output.txt")
        ```
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


@rule()
def build_all(_: OutPath = None):
    """Build all registered targets that have no dependents.

    Args:
        _ (OutPath | None): Placeholder parameter required for the rule
            decorator. It is ignored at runtime.

    Returns:
        None: Executes each rule whose output is not consumed by another rule.

    Examples:
        ```python
        from makeprov import build_all

        # Build every terminal target in the dependency graph
        build_all()
        ```
    """
    global RULES

    all_deps, all_outputs = set(), set()
    for rule in RULES.values():
        all_deps |= set(rule["deps"])
        all_outputs |= set(rule["outputs"])

    for target in all_outputs - all_deps:
        build(target)
