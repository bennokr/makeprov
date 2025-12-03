from __future__ import annotations

import functools
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, get_args, get_origin, get_type_hints

from parse import compile as parse_compile, Parser

from .config import ProvenanceConfig, ProvFormat, Frame, GLOBAL_CONFIG
from .paths import InDir, InPath, OutDir, OutPath
from .prov import Prov
from .rdfmixin import RDFMixin

try:
    import rdflib  # optional
except Exception:
    rdflib = None


@dataclass
class Rule:
    """Minimal description of a build rule.

    Rules capture the callable to execute, their declared dependencies and
    outputs, and optional parse templates for parameterized targets. Thin
    registry helpers in this module use these objects to resolve targets,
    explain the execution plan, and run builds.
    """

    name: str
    func: Callable
    deps: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    dep_templates: list[str] = field(default_factory=list)
    out_templates: list[str] = field(default_factory=list)
    out_parsers: list[Parser] = field(default_factory=list)
    phony: bool = False


# Rule registries
RULES_BY_TARGET: dict[str, Rule] = {}
RULES_BY_NAME: dict[str, Rule] = {}
PATTERN_RULES: list[Rule] = []
COMMANDS: set[Callable] = set()
PROV_BUFFERS: list[list[Prov]] = []


def _current_prov_buffer() -> list[Prov] | None:
    """Return the most recently started provenance buffer, if any."""

    if not PROV_BUFFERS:
        return None
    return PROV_BUFFERS[-1]


def start_prov_buffer() -> None:
    """Create a provenance buffer to batch writes.

    This function can be called multiple times to form a stack of provenance
    buffers. Nested buffers allow a rule decorated with ``merge=True`` to
    accumulate provenance from any rules it invokes and then merge that
    provenance into a single document.
    """

    PROV_BUFFERS.append([])


def flush_prov_buffer(
    *,
    prov_path: str | Path | None = None,
    config: ProvenanceConfig | None = None,
    fmt: ProvFormat | None = None,
    frame: Frame | None = None,
    context: bool | None = None,
) -> Prov | None:
    """Write or propagate the most recent provenance buffer.

    Returns the merged :class:`Prov` object for the flushed buffer. When a
    parent buffer exists, the merged provenance is appended to it for further
    aggregation; otherwise, the merged provenance is written to disk using the
    provided configuration (falling back to :data:`GLOBAL_CONFIG`).
    """

    if not PROV_BUFFERS:
        return None

    buffer = PROV_BUFFERS.pop()
    if not buffer:
        return None

    merged = Prov.merge(buffer)

    parent = _current_prov_buffer()
    if parent is not None:
        parent.append(merged)
        return merged

    cfg = config or GLOBAL_CONFIG
    destination = prov_path or cfg.prov_path or Path(cfg.prov_dir) / merged.name
    merged.write(
        destination,
        fmt=fmt if fmt is not None else cfg.out_fmt,
        frame=frame if frame is not None else cfg.frame,
        context=context if context is not None else cfg.context,
    )
    return merged


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
        .. code-block:: python

            from makeprov.core import needs_update

            if needs_update(["data/output.txt"], ["data/input.txt"]):
                regenerate()
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
        .. code-block:: python

            from typing import Optional
            from makeprov.core import _is_kind_annotation
            from makeprov.paths import InPath

            _is_kind_annotation(Optional[InPath], InPath)  # True
    """

    if ann is cls or (inspect.isclass(ann) and issubclass(ann, cls)):
        return True
    origin = get_origin(ann)
    if origin is None:
        return False
    return any(a is cls or (inspect.isclass(a) and issubclass(a, cls)) for a in get_args(ann))


def rule(
    *,
    name: str | None = None,
    phony: bool = False,
    base_iri: str | None = None,
    prov_dir: str | None = None,
    prov_path: str | None = None,
    force: bool | None = None,
    dry_run: bool | None = None,
    out_fmt: ProvFormat | None = None,
    frame: Frame | None = None,
    config: ProvenanceConfig | None = None,
    context: bool | None = None,
    merge: bool | None = None,
):
    """Decorate a function as a build rule with automatic provenance.

    Args:
        name (str | None): Logical name for the rule; defaults to the function
            name.
        phony (bool): When ``True``, do not require an :class:`OutPath`
            parameter and always execute the wrapped function regardless of
            timestamps. Useful for meta-rules such as aggregators or reporting
            commands.
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
        frame (Frame | None): Which structure to make primary subject of jsonld or 
            trig named graph. Options: `"provenance"` or `"results"`.
        config (ProvenanceConfig | None): Configuration object to use instead of
            :data:`makeprov.config.GLOBAL_CONFIG`.
        context (bool | None): Whether to embed JSON-LD context in output when
            writing provenance.
        merge (bool | None): When ``True``, buffer provenance for this rule and
            any nested rule calls, emitting a single merged document. Defaults
            to the configured merge behavior.

    Returns:
        Callable: A decorator that wraps the target function and registers it as
        a rule when outputs are discoverable from annotations. Templated
        :class:`InPath` or :class:`OutPath` defaults using ``str.format`` style
        placeholders (e.g. ``"data/{sample:d}.txt"``) register as pattern
        rules and are resolved dynamically for matching targets.

    Examples:
        Annotate parameters with :class:`InPath` and :class:`OutPath` to let the
        decorator infer dependencies:

        .. code-block:: python

            from makeprov import InPath, OutPath, rule

            @rule()
            def uppercase(src: InPath, dst: OutPath):
                dst.write_text(src.read_text().upper())

            uppercase("data/input.txt", "data/output.txt")
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

        if not out_params and not phony:
            raise ValueError(
                f"Function {func.__name__} needs an OutPath "
                f"parameter unless phony=True"
            )

        deps: list[str] = []
        outputs: list[str] = []
        dep_templates: list[str] = []
        out_templates: list[str] = []

        def is_template(s: str) -> bool:
            return "{" in s and "}" in s

        for p in sig.parameters.values():
            val = p.default

            if p.name in in_params and val is not inspect._empty:
                if isinstance(val, InPath):
                    s = str(val)
                    if is_template(s):
                        dep_templates.append(s)
                    elif not val.is_stream:
                        deps.append(s)
                elif isinstance(val, (str, Path)):
                    s = str(val)
                    if is_template(s):
                        dep_templates.append(s)
                    elif s != "-":
                        deps.append(s)

            if p.name in out_params and val is not inspect._empty:
                if isinstance(val, OutPath):
                    s = str(val)
                    if is_template(s):
                        out_templates.append(s)
                    elif not val.is_stream:
                        outputs.append(s)
                elif isinstance(val, (str, Path)):
                    s = str(val)
                    if is_template(s):
                        out_templates.append(s)
                    elif s != "-":
                        outputs.append(s)

        logical_name = name or func.__name__

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()

            fmt_kwargs = bound.arguments

            global GLOBAL_CONFIG
            base_config = config or GLOBAL_CONFIG
            rule_config = ProvenanceConfig(
                base_iri=base_iri if base_iri is not None else base_config.base_iri,
                prov_dir=prov_dir if prov_dir is not None else base_config.prov_dir,
                prov_path=base_config.prov_path,
                force=force if force is not None else base_config.force,
                dry_run=dry_run if dry_run is not None else base_config.dry_run,
                out_fmt=out_fmt if out_fmt is not None else base_config.out_fmt,
                frame=frame if frame is not None else base_config.frame,
                merge=merge if merge is not None else base_config.merge,
                context=context if context is not None else base_config.context,
            )

            in_files: list[Path] = []
            out_files: list[Path] = []

            def _format_if_template(val: str) -> str:
                if "{" in val and "}" in val:
                    return val.format(**fmt_kwargs)
                return val

            def normalize_in(pname: str) -> list[Path]:
                val = bound.arguments.get(pname)
                paths: list[Path] = []
                if isinstance(val, InPath):
                    s = _format_if_template(str(val))
                    new_val = val.__class__(s)
                    bound.arguments[pname] = new_val
                    if not new_val.is_stream:
                        paths.append(Path(s))
                elif val is None:
                    return paths
                else:
                    s = _format_if_template(str(val))
                    bound.arguments[pname] = type(val)(s) if s != val else val
                    if s != "-":
                        paths.append(Path(s))
                return paths

            def normalize_out(pname: str) -> list[Path]:
                val = bound.arguments.get(pname)
                paths: list[Path] = []
                if isinstance(val, OutPath):
                    s = _format_if_template(str(val))
                    new_val = val.__class__(s)
                    bound.arguments[pname] = new_val
                    if not new_val.is_stream:
                        paths.append(Path(s))
                elif val is None:
                    return paths
                else:
                    s = _format_if_template(str(val))
                    bound.arguments[pname] = type(val)(s) if s != val else val
                    if s != "-":
                        paths.append(Path(s))
                return paths

            for pname in in_params:
                in_files.extend(normalize_in(pname))

            for pname in out_params:
                out_files.extend(normalize_out(pname))

            for pname in in_params:
                val = bound.arguments.get(pname)
                if isinstance(val, InDir):
                    in_files.extend(Path(p) for p in val.children)

            if not phony and not rule_config.force and not needs_update(out_files, in_files):
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

            buffer_started = False
            if rule_config.merge:
                start_prov_buffer()
                buffer_started = True

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
                    for pname in in_params:
                        val = bound.arguments.get(pname)
                        if isinstance(val, InDir):
                            in_files.extend(Path(p) for p in val.children)

                    for pname in out_params:
                        val = bound.arguments.get(pname)
                        if isinstance(val, OutDir):
                            out_files.extend(Path(p) for p in val.children)

                    # Make sure results are a list
                    if isinstance(result, (list, tuple, set)):
                        results = result
                    else:
                        results = [result]

                    prov = Prov.create(
                        base_iri=rule_config.base_iri,
                        name=logical_name,
                        run_id=t0.strftime("%Y%m%dT%H%M"),
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

                    target_buffer = _current_prov_buffer()
                    if target_buffer is not None:
                        target_buffer.append(prov)
                    else:
                        prov.write(
                            rule_prov_path,
                            fmt=rule_config.out_fmt,
                            frame=rule_config.frame,
                            context=rule_config.context,
                        )

                    if buffer_started:
                        flush_prov_buffer(
                            prov_path=rule_prov_path,
                            config=rule_config,
                            fmt=rule_config.out_fmt,
                            frame=rule_config.frame,
                            context=rule_config.context,
                        )
                except Exception as prov_exc:  # noqa: BLE001
                    logging.warning(
                        "Failed to write provenance for %s: %s", logical_name, prov_exc
                    )

        rule_obj = Rule(
            name=logical_name,
            func=wrapped,
            deps=deps,
            outputs=outputs,
            dep_templates=dep_templates,
            out_templates=out_templates,
            out_parsers=[parse_compile(t) for t in out_templates],
            phony=phony,
        )

        RULES_BY_NAME[logical_name] = rule_obj

        if rule_obj.out_templates:
            PATTERN_RULES.append(rule_obj)
        else:
            for t in rule_obj.outputs:
                if t in RULES_BY_TARGET:
                    other = RULES_BY_TARGET[t]
                    raise ValueError(
                        f"Multiple rules produce {t!r}: {other.name!r} and {logical_name!r}"
                    )
                RULES_BY_TARGET[t] = rule_obj

        COMMANDS.add(wrapped)
        return wrapped

    return decorator


def resolve_target(target: str) -> tuple[Rule, dict[str, Any]]:
    """Resolve a target to its registered rule and parameters.

    Concrete targets are looked up directly in :data:`RULES_BY_TARGET`. Pattern
    rules are attempted in registration order using :mod:`parse` templates.
    """

    if target in RULES_BY_TARGET:
        return RULES_BY_TARGET[target], {}

    for rule_obj in PATTERN_RULES:
        for parser in rule_obj.out_parsers:
            match = parser.parse(target)
            if match is not None:
                return rule_obj, match.named

    raise RuntimeError(f"No rule to build target {target!r}")


def build(target: OutPath, _seen: set[str] | None = None, **kwargs):
    """Recursively build a target and its prerequisites.

    Args:
        target (OutPath): Path to the output to build. Paths may be concrete or
            match templated outputs registered with :func:`rule`.
        _seen (set[str] | None): Internal set to detect graph cycles.
    """

    top_level = _seen is None
    if _seen is None:
        _seen = set()

    target_str = str(target)
    if target_str in _seen:
        raise RuntimeError(f"Cycle in build graph at {target_str!r}")
    _seen.add(target_str)

    if top_level:
        start_prov_buffer()

    try:
        rule_obj, params = resolve_target(target_str)

        dep_paths = list(rule_obj.deps)
        for tmpl in rule_obj.dep_templates:
            dep_paths.append(tmpl.format(**params))

        for dep in dep_paths:
            try:
                resolve_target(dep)
            except RuntimeError:
                continue
            build(dep, _seen)

        rule_obj.func(**params)
    finally:
        if top_level:
            flush_prov_buffer(**kwargs)


def plan(target: str) -> list[tuple[str, Rule, dict[str, Any]]]:
    """Return the execution order for building a target.

    The plan is derived using :func:`resolve_target` for each dependency,
    ensuring concrete and templated rules are treated uniformly.
    """

    seen_targets: set[str] = set()
    visiting: set[str] = set()
    order: list[tuple[str, Rule, dict[str, Any]]] = []

    def dfs(t: str):
        if t in seen_targets:
            return
        if t in visiting:
            raise RuntimeError(f"Cycle in build graph at {t!r}")
        visiting.add(t)

        rule_obj, params = resolve_target(t)
        dep_paths = list(rule_obj.deps)
        for tmpl in rule_obj.dep_templates:
            dep_paths.append(tmpl.format(**params))

        for d in dep_paths:
            try:
                resolve_target(d)
            except RuntimeError:
                continue
            dfs(d)

        visiting.remove(t)
        seen_targets.add(t)
        order.append((t, rule_obj, params))

    dfs(target)
    return order


def explain(target: str) -> None:
    """Log the rule used for each target in build order."""

    for tgt, rule_obj, _ in plan(target):
        logging.info("target %s via rule %s", tgt, rule_obj.name)


def to_dot(target: str) -> str:
    """Render the dependency graph for ``target`` in DOT format."""

    edges: list[str] = []
    seen: set[tuple[str, str]] = set()

    for tgt, rule_obj, params in plan(target):
        dep_paths = list(rule_obj.deps)
        for tmpl in rule_obj.dep_templates:
            dep_paths.append(tmpl.format(**params))

        for dep in dep_paths:
            try:
                resolve_target(dep)
            except RuntimeError:
                continue
            if (dep, rule_obj.name) not in seen:
                edges.append(f'"{dep}" -> "{rule_obj.name}";')
                seen.add((dep, rule_obj.name))

        outputs = list(rule_obj.outputs)
        for tmpl in rule_obj.out_templates:
            outputs.append(tmpl.format(**params))

        for out in outputs:
            if (rule_obj.name, out) not in seen:
                edges.append(f'"{rule_obj.name}" -> "{out}";')
                seen.add((rule_obj.name, out))

    return "digraph workflow {\n  " + "\n  ".join(edges) + "\n}"


def list_rules() -> list[str]:
    """Return registered rule names in alphabetical order."""

    return sorted(RULES_BY_NAME.keys())


def list_targets() -> list[str]:
    """Return concrete targets produced by non-pattern rules."""

    return sorted(RULES_BY_TARGET.keys())


def root_targets() -> list[str]:
    """Return concrete targets that are not dependencies of other rules."""

    concrete = set(RULES_BY_TARGET.keys())
    deps: set[str] = set()
    for rule_obj in RULES_BY_TARGET.values():
        deps |= set(rule_obj.deps)
    return sorted(concrete - deps)


def dry_run_build(target: str) -> None:
    """Log the steps required to build ``target`` without executing rules."""

    for tgt, rule_obj, _ in plan(target):
        logging.info("would run rule %s for target %s", rule_obj.name, tgt)


def build_all():
    """Build all concrete targets that have no dependents."""

    for target in root_targets():
        build(target)
