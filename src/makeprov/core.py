from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import sys
import tempfile
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, get_args, get_origin, get_type_hints
from urllib.parse import quote

from parse import compile as parse_compile, Parser

from .config import ProvenanceConfig, ProvFormat, Frame
from .paths import CachedDownload, InDir, InPath, OutDir, OutPath
from .prov import (
    COMMON_CONTEXT, PlanGraph, Prov, ProvenanceWriteError,
    _caller_script, _safe_cmd, resolve_iris,
)
from .meta import ProvMeta
from .rdfmixin import RDFMixin
from .refs import ArtifactRef

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


@dataclass
class Session:
    """In-memory registries and buffers for a makeprov run."""

    rules_by_target: dict[str, Rule] = field(default_factory=dict)
    rules_by_name: dict[str, Rule] = field(default_factory=dict)
    pattern_rules: list[Rule] = field(default_factory=list)
    commands: set[Callable] = field(default_factory=set)
    prov_buffers: list[list[Prov]] = field(default_factory=list)
    prov_stream: _Stream | None = None
    active_activities: list[str] = field(default_factory=list)


@dataclass
class _Stream:
    config: ProvenanceConfig
    path: Path | None = None
    started: bool = False
    failed: bool = False


def _stream_line(stream: _Stream, data: dict, destination: Path) -> None:
    """Append a complete JSON-LD document; never overwrite crash evidence."""
    if stream.path is None:
        stream.path = destination.with_suffix(".jsonl")
    stream.path.parent.mkdir(parents=True, exist_ok=True)
    with stream.path.open("a" if stream.started else "x", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")
    stream.started = True


def _write_merged(prov: Prov, destination: Path, cfg: ProvenanceConfig) -> Path:
    """Atomically replace merged provenance, keeping JSONL until it succeeds."""
    final = destination.with_suffix(".json" if cfg.out_fmt == "json" else ".trig")
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=final.parent, suffix=final.suffix)
    os.close(fd)
    try:
        written = prov.write(temporary, fmt=cfg.out_fmt, frame=cfg.frame,
                             context=cfg.context, context_url=cfg.context_url)
        os.replace(written, final)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return final


# A process-wide Session used implicitly by makeprov.rule()/build() when no
# `session=` is passed. This is a convenience for scripts and notebooks;
# prefer makeprov.new_session() for tests, multiprocessing, or embedding
# makeprov in a larger application, where a shared process-wide registry is
# the wrong default.
_DEFAULT_SESSION = Session()

# Read-only-by-convention references into the default session, kept for
# introspection (e.g. `len(makeprov.COMMANDS)`). Mutate via `rule()`, not
# these directly.
RULES_BY_TARGET = _DEFAULT_SESSION.rules_by_target
RULES_BY_NAME = _DEFAULT_SESSION.rules_by_name
PATTERN_RULES = _DEFAULT_SESSION.pattern_rules
COMMANDS = _DEFAULT_SESSION.commands
PROV_BUFFERS = _DEFAULT_SESSION.prov_buffers


def _get_session(session: Session | None = None) -> Session:
    """Return a provided session or the default shared session."""

    return session or _DEFAULT_SESSION


def new_session() -> Session:
    """Create a fresh session with isolated registries and buffers."""

    return Session()


def _current_prov_buffer(session: Session | None = None) -> list[Prov] | None:
    """Return the most recently started provenance buffer for ``session``."""

    sess = _get_session(session)
    if not sess.prov_buffers:
        return None
    return sess.prov_buffers[-1]


def start_prov_buffer(*, session: Session | None = None,
                      config: ProvenanceConfig | None = None) -> None:
    """Create a provenance buffer to batch writes.

    Buffers capture provenance from multiple rule invocations and emit a single
    merged document when flushed. Callers should prefer one top-level buffer per
    workflow run; nested buffers are supported for backward compatibility but
    are avoided by the public APIs to keep merge semantics predictable.
    """

    sess = _get_session(session)
    sess.prov_buffers.append([])
    cfg = config or ProvenanceConfig.get()
    if cfg.stream and sess.prov_stream is None:
        sess.prov_stream = _Stream(cfg)
    logging.debug("Started provenance buffer (depth=%d)", len(sess.prov_buffers))


def flush_prov_buffer(
    *,
    prov_path: str | Path | None = None,
    config: ProvenanceConfig | None = None,
    fmt: ProvFormat | None = None,
    frame: Frame | None = None,
    context: bool | None = None,
    context_url: str | None = None,
    session: Session | None = None,
    label: str | None = None,
    success: bool = True,
) -> Prov | None:
    """Write or propagate the most recent provenance buffer.

    Returns the merged :class:`Prov` object for the flushed buffer. When a
    parent buffer exists, the merged provenance is appended to it for further
    aggregation. When ``prov_path`` is provided, the merged provenance is
    written even if a parent buffer exists. Without a parent buffer, the merged
    provenance is written to disk using the provided configuration (falling
    back to the process-wide configuration via :class:`makeprov.ProvenanceConfig`).
    """

    sess = _get_session(session)
    if not sess.prov_buffers:
        return None

    buffer = sess.prov_buffers.pop()
    if not buffer:
        if not sess.prov_buffers:
            sess.prov_stream = None
        return None

    merged = Prov.merge(buffer)
    if label:
        merged.name = label

    cfg = config or (sess.prov_stream.config if sess.prov_stream else ProvenanceConfig.get())
    fmt_val = fmt if fmt is not None else cfg.out_fmt
    frame_val = frame if frame is not None else cfg.frame
    context_val = context if context is not None else cfg.context
    context_url_val = context_url if context_url is not None else cfg.context_url
    logging.debug("Merged %d provenance records", len(buffer))

    parent = _current_prov_buffer(sess)
    if parent is not None:
        parent.append(merged)
        logging.debug(
            "Appended merged provenance to parent buffer (depth=%d)", len(sess.prov_buffers)
        )
        if prov_path is not None:
            logging.debug(
                "Writing nested provenance to %s (fmt=%s, frame=%s, context=%s, context_url=%s)",
                prov_path,
                fmt_val,
                frame_val,
                context_val,
                context_url_val,
            )
            merged.write(
                prov_path,
                fmt=fmt_val,
                frame=frame_val,
                context=context_val,
                context_url=context_url_val,
            )
        return merged

    destination = prov_path or cfg.prov_path or Path(cfg.prov_dir) / merged.name
    stream = sess.prov_stream
    sess.prov_stream = None
    if stream is not None:
        if not success or stream.failed or not cfg.merge:
            return merged  # Preserve JSONL after failure or when merge=False.
        _write_merged(merged, Path(destination), replace(
            cfg, out_fmt=fmt_val, frame=frame_val,
            context=context_val, context_url=context_url_val,
        ))
        if stream.started:
            stream.path.unlink()
        return merged
    logging.debug(
        "Flushing provenance to %s (fmt=%s, frame=%s, context=%s, context_url=%s)",
        destination,
        fmt_val,
        frame_val,
        context_val,
        context_url_val,
    )
    merged.write(
        destination,
        fmt=fmt_val,
        frame=frame_val,
        context=context_val,
        context_url=context_url_val,
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


def _path_ref(path: Path) -> ArtifactRef:
    """Convert a declared path into an :class:`ArtifactRef`."""

    extra: dict[str, Any] = {}
    if isinstance(path, CachedDownload):
        # As {"@id": ...}, not a bare string: prov:wasDerivedFrom and
        # rdfs:seeAlso both range over resources, so a plain string would
        # serialize as a literal and the link would not be traversable.
        extra[path.transform] = {"@id": path.url}
        extra.setdefault("rdfs:seeAlso", {"@id": path.url})
        if path.headers:
            extra["comment"] = f"download headers={path.headers}"
    return ArtifactRef.local(path, extra=extra)


def _plan_graph(rule_name: str, inputs: list[Path], session: Session) -> PlanGraph:
    """Describe the executing rule and the rules producing its inputs.

    Uses the same resolver as :func:`build`, so the prospective structure agrees
    with what would actually be built.
    """

    requires: list[str] = []
    for path in inputs:
        try:
            upstream, _ = resolve_target(str(path), session=session)
        except RuntimeError:
            continue  # not produced by a rule: a source file, not a step
        if upstream.name != rule_name and upstream.name not in requires:
            requires.append(upstream.name)
    return PlanGraph(rule=rule_name, requires=tuple(requires))


def _run_id(config: ProvenanceConfig, t0: datetime) -> str:
    """Mint a unique identifier for one run of a rule.

    A timestamp alone is not unique. Minute-resolution stamps meant two runs of
    the same rule within the same minute produced identical activity IRIs and
    collapsed into a single node. ``config.run_id`` lets a caller supply an
    external run identity instead, such as a CI job or MLflow run id.
    """

    if config.run_id:
        return config.run_id
    return f"{t0:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"


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
    stream: bool | None = None,
    strict: bool | None = None,
    session: Session | None = None,
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
            the process-wide configuration returned by
            :class:`makeprov.ProvenanceConfig`.
        context (bool | None): Whether to embed JSON-LD context in output when
            writing provenance.
        merge (bool | None): When ``True``, buffer provenance for this rule and
            any nested rule calls, emitting a single merged document. Defaults
            to the configured merge behavior.
        stream (bool | None): Append JSON-LD records to a recovery JSONL file;
            merge and remove it on successful completion when ``merge=True``.
            Like ``merge``, this propagates: once a rule starts streaming for
            the active buffer, every nested rule call streams into it too,
            regardless of that nested rule's own ``stream`` setting.
        strict (bool | None): When ``True`` (the default), a failure to write
            provenance raises :class:`~makeprov.prov.ProvenanceWriteError`
            instead of only logging a warning. Overrides the configured
            strict behavior when set.
        session (Session | None): Registry and buffer container to use instead
            of the process-wide default session. Passing a dedicated session
            isolates rules, commands, and provenance buffers from other runs.

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
        sess = _get_session(session)
        hints = get_type_hints(func)

        in_params: list[str] = []
        out_params: list[str] = []
        ref_params: list[str] = []
        meta_params: list[str] = []
        for p in sig.parameters.values():
            ann = hints.get(p.name, p.annotation)
            if _is_kind_annotation(ann, InPath):
                in_params.append(p.name)
            if _is_kind_annotation(ann, OutPath):
                out_params.append(p.name)
            if _is_kind_annotation(ann, ArtifactRef):
                ref_params.append(p.name)
            if get_origin(ann) is ProvMeta:
                meta_params.append(p.name)

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

            base_config = config or ProvenanceConfig.get()
            rule_config = ProvenanceConfig(
                base_iri=base_iri if base_iri is not None else base_config.base_iri,
                prov_dir=prov_dir if prov_dir is not None else base_config.prov_dir,
                prov_path=base_config.prov_path,
                force=force if force is not None else base_config.force,
                dry_run=dry_run if dry_run is not None else base_config.dry_run,
                out_fmt=out_fmt if out_fmt is not None else base_config.out_fmt,
                frame=frame if frame is not None else base_config.frame,
                merge=merge if merge is not None else base_config.merge,
                stream=stream if stream is not None else base_config.stream,
                context=context if context is not None else base_config.context,
                context_url=base_config.context_url,
                strict=strict if strict is not None else base_config.strict,
                run_id=base_config.run_id,
                record_user=base_config.record_user,
                record_environment=base_config.record_environment,
                forge_profiles=base_config.forge_profiles,
                emit_plan_graph=base_config.emit_plan_graph,
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
                    if isinstance(val, CachedDownload):
                        new_val = val.__class__(
                            val.url,
                            s,
                            headers=val.headers,
                            transform=val.transform,
                        )
                    else:
                        new_val = val.__class__(s)
                    bound.arguments[pname] = new_val
                    if not new_val.is_stream:
                        paths.append(new_val)
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
                        paths.append(new_val)
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
                    in_files.extend(Path(p) for p in val.all_children())

            # External references never participate in staleness checks: they
            # have no local mtime to compare against.
            extra_refs: list[ArtifactRef] = []
            for pname in ref_params:
                val = bound.arguments.get(pname)
                if isinstance(val, ArtifactRef):
                    extra_refs.append(val)

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
            if (rule_config.merge or rule_config.stream) and _current_prov_buffer(sess) is None:
                start_prov_buffer(session=sess, config=rule_config)
                buffer_started = True
            if rule_config.stream and sess.prov_stream is None:
                sess.prov_stream = _Stream(rule_config)

            t0 = datetime.now(timezone.utc)
            run_id = _run_id(rule_config, t0)
            rule_prov_path = Path(prov_path or rule_config.prov_path or
                                  Path(rule_config.prov_dir) / logical_name)
            stream_state = sess.prov_stream
            parent_id = sess.active_activities[-1] if sess.active_activities else None
            activity_id = None
            git_origin = git_revision = None
            if stream_state is not None:
                # Resolve once and hand the same origin/revision to Prov.create
                # below, so it mints identifiers from a minter with the same
                # policy instead of re-running these git lookups.
                git_origin = _safe_cmd(["git", "config", "--get", "remote.origin.url"])
                git_revision = _safe_cmd(["git", "rev-parse", "HEAD"])
                iri_context = deepcopy(COMMON_CONTEXT)
                minter = resolve_iris(rule_config.base_iri, iri_context,
                                      origin=git_origin, revision=git_revision,
                                      forge_profiles=rule_config.forge_profiles)
                activity_id = minter.file(
                    f"{quote(_caller_script().name, safe='')}#{logical_name}-{run_id}"
                )
                if parent_id is None:
                    try:
                        _stream_line(stream_state, {
                            "@context": iri_context, "id": activity_id,
                            "type": "prov:Activity", "startedAtTime": t0.isoformat(),
                        }, rule_prov_path)
                    except BaseException:
                        if buffer_started:
                            sess.prov_buffers.pop()
                            sess.prov_stream = None
                        raise
                sess.active_activities.append(activity_id)

            exc: BaseException | None = None
            result = None

            try:
                result = func(*bound.args, **bound.kwargs)
                return result
            except BaseException as e:
                exc = e
                if stream_state is not None:
                    stream_state.failed = True
                raise
            finally:
                t1 = datetime.now(timezone.utc)
                try:
                    for pname in in_params:
                        val = bound.arguments.get(pname)
                        if isinstance(val, InDir):
                            in_files.extend(Path(p) for p in val.all_children())

                    for pname in out_params:
                        val = bound.arguments.get(pname)
                        if isinstance(val, OutDir):
                            out_files.extend(Path(p) for p in val.all_children())

                    # Make sure results are a list
                    if isinstance(result, (list, tuple, set)):
                        results = result
                    else:
                        results = [result]

                    prov = Prov.create(
                        base_iri=rule_config.base_iri,
                        name=logical_name,
                        run_id=run_id,
                        t0=t0,
                        t1=t1,
                        inputs=[_path_ref(p) for p in in_files] + extra_refs,
                        outputs=[_path_ref(p) for p in out_files],
                        results=results,
                        success=exc is None,
                        metadata={key: bound.arguments[key] for key in meta_params
                                  if key in bound.arguments},
                        activity_id=activity_id,
                        parent_id=parent_id,
                        origin=git_origin,
                        revision=git_revision,
                        record_user=rule_config.record_user,
                        record_environment=rule_config.record_environment,
                        forge_profiles=rule_config.forge_profiles,
                        plan_graph=(
                            _plan_graph(logical_name, in_files, sess)
                            if rule_config.emit_plan_graph
                            else None
                        ),
                    )
                    target_buffer = _current_prov_buffer(sess)
                    if target_buffer is not None:
                        target_buffer.append(prov)
                    elif stream_state is None:
                        prov.write(
                            rule_prov_path,
                            fmt=rule_config.out_fmt,
                            frame=rule_config.frame,
                            context=rule_config.context,
                            context_url=rule_config.context_url,
                        )
                    if stream_state is not None:
                        _stream_line(stream_state, prov.to_jsonld(with_context=True),
                                     rule_prov_path)

                    if buffer_started:
                        flush_prov_buffer(
                            prov_path=rule_prov_path,
                            config=rule_config,
                            fmt=rule_config.out_fmt,
                            frame=rule_config.frame,
                            context=rule_config.context,
                            context_url=rule_config.context_url,
                            session=sess,
                            success=exc is None,
                        )
                except Exception as prov_exc:  # noqa: BLE001
                    if stream_state is not None:
                        stream_state.failed = True
                    if buffer_started and sess.prov_buffers:
                        flush_prov_buffer(config=rule_config, session=sess, success=False)
                    if rule_config.strict:
                        # Already a provenance error with a precise message
                        # (e.g. an unresolved output); don't bury it in a wrapper.
                        if isinstance(prov_exc, ProvenanceWriteError):
                            raise
                        raise ProvenanceWriteError(
                            f"Failed to write provenance for {logical_name!r}: {prov_exc}"
                        ) from prov_exc
                    logging.warning(
                        "Failed to write provenance for %s: %s", logical_name, prov_exc
                    )
                finally:
                    if activity_id is not None:
                        sess.active_activities.pop()

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

        sess.rules_by_name[logical_name] = rule_obj

        if rule_obj.out_templates:
            sess.pattern_rules.append(rule_obj)
        else:
            for t in rule_obj.outputs:
                if t in sess.rules_by_target:
                    other = sess.rules_by_target[t]
                    raise ValueError(
                        f"Multiple rules produce {t!r}: {other.name!r} and {logical_name!r}"
                    )
                sess.rules_by_target[t] = rule_obj

        sess.commands.add(wrapped)
        return wrapped

    return decorator


def resolve_target(target: str, *, session: Session | None = None) -> tuple[Rule, dict[str, Any]]:
    """Resolve a target to its registered rule and parameters.

    Concrete targets are looked up directly in :data:`RULES_BY_TARGET`. Pattern
    rules are attempted in registration order using :mod:`parse` templates.
    """

    sess = _get_session(session)

    if target in sess.rules_by_target:
        return sess.rules_by_target[target], {}

    for rule_obj in sess.pattern_rules:
        for parser in rule_obj.out_parsers:
            match = parser.parse(target)
            if match is not None:
                return rule_obj, match.named

    raise RuntimeError(f"No rule to build target {target!r}")


def build(
    target: OutPath,
    _seen: set[str] | None = None,
    *,
    session: Session | None = None,
    **kwargs,
):
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
    sess = _get_session(session)
    if target_str in _seen:
        raise RuntimeError(f"Cycle in build graph at {target_str!r}")
    _seen.add(target_str)

    buffer_started = False
    top_config = ProvenanceConfig.get()
    if top_level and (top_config.merge or top_config.stream) and _current_prov_buffer(sess) is None:
        start_prov_buffer(session=sess, config=top_config)
        buffer_started = True

    try:
        rule_obj, params = resolve_target(target_str, session=sess)

        dep_paths = list(rule_obj.deps)
        for tmpl in rule_obj.dep_templates:
            dep_paths.append(tmpl.format(**params))

        for dep in dep_paths:
            try:
                resolve_target(dep, session=sess)
            except RuntimeError:
                continue
            build(dep, _seen, session=sess)

        rule_obj.func(**params)
    finally:
        if top_level and buffer_started:
            kwargs.setdefault("success", sys.exc_info()[0] is None)
            flush_prov_buffer(session=sess, **kwargs)


def plan(target: str, *, session: Session | None = None) -> list[tuple[str, Rule, dict[str, Any]]]:
    """Return the execution order for building a target.

    The plan is derived using :func:`resolve_target` for each dependency,
    ensuring concrete and templated rules are treated uniformly.
    """

    sess = _get_session(session)
    seen_targets: set[str] = set()
    visiting: set[str] = set()
    order: list[tuple[str, Rule, dict[str, Any]]] = []

    def dfs(t: str):
        if t in seen_targets:
            return
        if t in visiting:
            raise RuntimeError(f"Cycle in build graph at {t!r}")
        visiting.add(t)

        rule_obj, params = resolve_target(t, session=sess)
        dep_paths = list(rule_obj.deps)
        for tmpl in rule_obj.dep_templates:
            dep_paths.append(tmpl.format(**params))

        for d in dep_paths:
            try:
                resolve_target(d, session=sess)
            except RuntimeError:
                continue
            dfs(d)

        visiting.remove(t)
        seen_targets.add(t)
        order.append((t, rule_obj, params))

    dfs(target)
    return order


def explain(target: str, *, session: Session | None = None) -> None:
    """Log the rule used for each target in build order."""

    for tgt, rule_obj, _ in plan(target, session=session):
        logging.info("target %s via rule %s", tgt, rule_obj.name)


def to_dot(target: str, *, session: Session | None = None) -> str:
    """Render the dependency graph for ``target`` in DOT format."""

    edges: list[str] = []
    seen: set[tuple[str, str]] = set()

    sess = _get_session(session)

    for tgt, rule_obj, params in plan(target, session=sess):
        dep_paths = list(rule_obj.deps)
        for tmpl in rule_obj.dep_templates:
            dep_paths.append(tmpl.format(**params))

        for dep in dep_paths:
            try:
                resolve_target(dep, session=sess)
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


def list_rules(*, session: Session | None = None) -> list[str]:
    """Return registered rule names in alphabetical order."""

    return sorted(_get_session(session).rules_by_name.keys())


def list_targets(*, session: Session | None = None) -> list[str]:
    """Return concrete targets produced by non-pattern rules."""

    return sorted(_get_session(session).rules_by_target.keys())


def root_targets(*, session: Session | None = None) -> list[str]:
    """Return concrete targets that are not dependencies of other rules."""

    sess = _get_session(session)
    concrete = set(sess.rules_by_target.keys())
    deps: set[str] = set()
    for rule_obj in sess.rules_by_target.values():
        deps |= set(rule_obj.deps)
    return sorted(concrete - deps)


def dry_run_build(target: str, *, session: Session | None = None) -> None:
    """Log the steps required to build ``target`` without executing rules."""

    for tgt, rule_obj, _ in plan(target, session=session):
        logging.info("would run rule %s for target %s", rule_obj.name, tgt)


def build_all(*, session: Session | None = None):
    """Build all concrete targets that have no dependents."""

    sess = _get_session(session)
    for target in root_targets(session=sess):
        build(target, session=sess)
