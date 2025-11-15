from __future__ import annotations

import functools
import logging
import mimetypes
import subprocess
import sys
import inspect
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from typing import get_origin, get_args, get_type_hints, Any
from abc import ABC, abstractmethod
from collections.abc import Callable
import importlib.metadata as im

import rdflib
from rdflib import RDF, RDFS, XSD
from rdflib.namespace import DCTERMS as DCT

PROV = rdflib.Namespace("http://www.w3.org/ns/prov#")

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


@dataclass
class ProvenanceConfig:
    base_iri: str = "http://example.org/"
    prov_dir: str = "prov"
    force: bool = False  # if True, run even when up to date
    dry_run: bool = False  # if True, do not run, just log


GLOBAL_CONFIG = ProvenanceConfig()


# ----------------------------------------------------------------------
# File marker hierarchy (with "-" as stdin/stdout)
# ----------------------------------------------------------------------


class File(ABC):
    """Abstract base for InFile / OutFile.

    Common fields:
      - raw: original string
      - path: filesystem path or None (for streams)
      - is_stream: True if "-" (stdin/stdout)
      - stream_name: "stdin" / "stdout" / None
    """

    def __init__(self, path: str | Path, stream_name: str | None):
        raw = str(path)
        self.raw = raw

        if raw == "-":
            self.is_stream = True
            self.stream_name = stream_name
            self.path: Path | None = None
        else:
            self.is_stream = False
            self.stream_name = None
            self.path = Path(path)

    def __fspath__(self):
        if self.is_stream or self.path is None:
            raise TypeError(
                f"{self.__class__.__name__}('-') does not have a filesystem path"
            )
        return str(self.path)

    def __str__(self) -> str:
        return self.raw

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.raw!r})"

    @abstractmethod
    def open(self, mode: str = "", *args, **kwargs):
        """Open underlying file or stream."""
        ...


class InFile(File):
    """Marker type for input files (for provenance + DAG).

    The string "-" stands for stdin.
    """

    def __init__(self, path: str):
        super().__init__(path, stream_name="stdin")

    def open(self, mode: str = "r", *args, **kwargs):
        if self.is_stream:
            return sys.stdin.buffer if "b" in mode else sys.stdin
        if self.path is None:
            raise ValueError("InFile has no path")
        
        return self.path.open(mode, *args, **kwargs)


class OutFile(File):
    """Marker type for output files (for provenance + DAG).

    The string "-" stands for stdout.
    """

    def __init__(self, path: str):
        super().__init__(path, stream_name="stdout")

    def as_infile(self) -> InFile:
        """Convert an OutFile path into a new InFile for downstream steps."""
        if self.is_stream or self.path is None:
            raise ValueError("Cannot convert a stream-based OutFile into an InFile")
        return InFile(str(self.path))

    def open(self, mode: str = "w", *args, **kwargs):
        if self.is_stream:
            return sys.stdout.buffer if "b" in mode else sys.stdout
        if self.path is None:
            raise ValueError("OutFile has no path")
        
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open(mode, *args, **kwargs)


# ----------------------------------------------------------------------
# Registry + basic Make-like build
# ----------------------------------------------------------------------

RULES: dict[str, dict[str, Any]] = {}
COMMANDS: set[Callable] = set()

def _caller_script() -> Path:
    mod = sys.modules.get("__main__")
    if getattr(mod, "__file__", None):
        return Path(mod.__file__).resolve()

    if sys.argv and sys.argv[0]:
        p = Path(sys.argv[0])
        if p.exists():
            return p.resolve()

    for f in reversed(inspect.stack()):
        p = Path(f.filename)
        if p.suffix in {".py", ""}:
            return p.resolve()

    return Path("unknown")


def _safe_cmd(argv: list[str]) -> str | None:
    try:
        return subprocess.run(
            argv, check=True, capture_output=True, text=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def needs_update(outputs, deps) -> bool:
    """Return True if outputs missing or older than any dependency."""
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

    `target` is a path (string/Path). Only rules with default
    OutFile paths are part of this DAG.
    """
    if _seen is None:
        _seen = set()
    target = str(target)

    if target in _seen:
        raise RuntimeError(f"Cycle in build graph at {target!r}")
    _seen.add(target)

    rule = RULES[target]

    for dep in rule["deps"]:
        if dep in RULES:
            build(dep, _seen)

    rule["func"]()


# ----------------------------------------------------------------------
# PROV helpers
# ----------------------------------------------------------------------


def _describe_file(g: rdflib.Graph, base: rdflib.Namespace, path: Path, kind: str):
    iri = base[f"{kind}/{path.as_posix()}"]

    mtype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    size = path.stat().st_size if path.exists() else 0
    mtime = (
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        if path.exists()
        else None
    )

    g.add((iri, RDF.type, PROV.Entity))
    g.add((iri, DCT.format, rdflib.Literal(mtype)))
    g.add((iri, DCT.extent, rdflib.Literal(size, datatype=XSD.integer)))
    if mtime:
        g.add((iri, DCT.modified, rdflib.Literal(mtime, datatype=XSD.dateTime)))

    if kind == "src" and path.exists():
        try:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            g.add((iri, DCT.identifier, rdflib.Literal(f"sha256:{sha}")))
        except Exception:  # noqa: BLE001
            pass

    return iri


def project_metadata(dist_name: str | None = None):
    """
    Return (name, version, requires) using importlib.metadata.

    If dist_name is None, tries to infer from the caller module.
    """
    if dist_name is None:
        # crude default: module name of the caller
        frame = inspect.stack()[1]
        module = inspect.getmodule(frame[0])
        if module and module.__package__:
            dist_name = module.__package__.split(".", 1)[0]
        else:
            return None, None, []

    try:
        dist = im.distribution(dist_name)
    except im.PackageNotFoundError:
        return None, None, []

    name = dist.metadata.get("Name")
    version = dist.version
    requires = dist.requires or []
    return name, version, requires

def pep503_normalize(name: str) -> str:
    """PEP 503 normalization: lowercase, runs of [-_.] -> '-'."""
    name = name.strip()
    name = name.lower()
    return re.sub(r"[-_.]+", "-", name)


def add_deps_to_env(
    D: rdflib.Graph,
    env_iri: rdflib.term.Identifier,
    deps_specs: list[str],
):
    """
    Attach dependencies from deps_specs to env_iri using PyPI-based IRIs.

    deps_specs: e.g. ["rdflib>=6.0.0", "pydantic==2.0.3"]
    """
    for spec in deps_specs:
        spec_str = spec.strip()
        if not spec_str:
            continue

        pkg = spec_str.split()[0]
        # keep only the name part before version operator
        pkg_name = re.split(r"[<>=!~ ]", pkg, 1)[0]
        norm = pep503_normalize(pkg_name)

        dep_iri = rdflib.URIRef(f"https://pypi.org/project/{norm}/")

        D.add((dep_iri, RDF.type, RDF.Resource))
        D.add((dep_iri, RDFS.label, rdflib.Literal(spec_str)))
        # link environment -> dependency
        D.add((env_iri, DCT.requires, dep_iri))

def _augment_with_metadata(
    D: rdflib.Graph,
    base: rdflib.Namespace,
    activity: rdflib.term.Identifier,
    run_id: str,
):
    """Add metadata + dependency info to provenance, if possible."""
    name, version, deps_specs = project_metadata()

    if name or version or deps_specs:
        env = base[f"env/{run_id}"]
        D.add((env, RDF.type, PROV.Entity))
        D.add((env, RDF.type, PROV.Collection))
        D.add((env, RDFS.label, rdflib.Literal("Python environment")))
        D.add((activity, PROV.used, env))

        if name:
            D.add((env, DCT.title, rdflib.Literal(name)))
        if version:
            D.add((env, DCT.hasVersion, rdflib.Literal(version)))

        add_deps_to_env(D, env, deps_specs)


def _write_provenance_dataset(
    base_iri: str,
    name: str,
    prov_path: str | Path,
    deps: list[str],
    outputs: list[str],
    t0: datetime,
    t1: datetime,
    data_graph: rdflib.Graph | None = None,
    success: bool = True,
):
    """
    Build a Dataset with:
      - default graph: PROV metadata
      - named graph:   data_graph (if provided)
    and serialize as Trig to prov_path.
    """
    base = rdflib.Namespace(base_iri)
    ds = rdflib.Dataset()
    D = ds.default_context

    ds.bind("", base)
    ds.bind("prov", PROV)
    ds.bind("dcterms", DCT)

    run_id = t0.strftime("%Y%m%dT%H%M%S")
    script = _caller_script()

    activity = base[f"run/{name}/{run_id}"]
    agent = base[f"agent/{script.name}"]
    graph_iri = base[f"graph/{name}"]

    commit = _safe_cmd(["git", "rev-parse", "HEAD"])
    origin = _safe_cmd(["git", "config", "--get", "remote.origin.url"])

    D.add((activity, RDF.type, PROV.Activity))
    t0_term = rdflib.Literal(t0.isoformat(), datatype=XSD.dateTime)
    D.add((activity, PROV.startedAtTime, t0_term))
    t1_term = rdflib.Literal(t1.isoformat(), datatype=XSD.dateTime)
    D.add((activity, PROV.endedAtTime, t1_term))

    D.add((agent, RDF.type, PROV.SoftwareAgent))
    D.add((agent, RDFS.label, rdflib.Literal(script.name)))
    if commit:
        D.add((agent, DCT.hasVersion, rdflib.Literal(commit)))
    if origin:
        D.add((agent, DCT.source, rdflib.URIRef(origin)))
    D.add((activity, PROV.wasAssociatedWith, agent))

    if data_graph is not None:
        gx = ds.get_context(graph_iri)
        for triple in data_graph:
            gx.add(triple)

        D.add((graph_iri, RDF.type, PROV.Entity))
        D.add((graph_iri, PROV.wasGeneratedBy, activity))
        D.add((graph_iri, PROV.wasAttributedTo, agent))
        D.add((graph_iri, PROV.generatedAtTime, t1_term))

    for d in deps:
        p = Path(d)
        if not p.exists():
            continue
        src = _describe_file(D, base, p, "src")
        D.add((activity, PROV.used, src))

    for o in outputs:
        p = Path(o)
        if not p.exists():
            continue
        ent = _describe_file(D, base, p, "out")
        D.add((ent, PROV.wasGeneratedBy, activity))

    if not success:
        D.add((activity, RDFS.comment, rdflib.Literal("task failed")))

    # Add pyproject + dependency information, if available
    _augment_with_metadata(D, base, activity, run_id)

    prov_path = Path(prov_path)
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Writing provenance dataset %s", prov_path)
    ds.serialize(prov_path, format="trig")


# ----------------------------------------------------------------------
# Annotation helpers (supports Optional[InFile], OutFile | None, etc.)
# ----------------------------------------------------------------------


def _is_kind_annotation(ann: Any, cls: type) -> bool:
    if ann is cls:
        return True

    origin = get_origin(ann)
    if origin is None:
        return False

    return any(a is cls for a in get_args(ann))


# ----------------------------------------------------------------------
# Decorator: infer inputs/outputs from signature
# ----------------------------------------------------------------------


def rule(
    *,
    name: str | None = None,
    base_iri: str | None = None,
    prov_dir: str | None = None,
    prov_path: str | None = None,
    force: bool | None = None,
    dry_run: bool | None = None,
    config: ProvenanceConfig | None = None,
):
    base_config = config or GLOBAL_CONFIG

    rule_config = ProvenanceConfig(
        base_iri=base_iri if base_iri is not None else base_config.base_iri,
        prov_dir=prov_dir if prov_dir is not None else base_config.prov_dir,
        force=force if force is not None else base_config.force,
        dry_run=dry_run if dry_run is not None else base_config.dry_run,
    )

    def decorator(func):
        sig = inspect.signature(func)
        hints = get_type_hints(func)

        in_params: list[str] = []
        out_params: list[str] = []

        for p in sig.parameters.values():
            ann = hints.get(p.name, p.annotation)
            if _is_kind_annotation(ann, InFile):
                in_params.append(p.name)
            if _is_kind_annotation(ann, OutFile):
                out_params.append(p.name)

        if not out_params:
            raise ValueError(
                f"Function {func.__name__} must have at least one "
                f"OutFile (possibly Optional[OutFile]) parameter"
            )

        deps: list[str] = []
        outputs: list[str] = []

        for p in sig.parameters.values():
            if p.name in in_params and p.default is not inspect._empty:
                val = p.default
                if isinstance(val, InFile):
                    if getattr(val, "is_stream", False):
                        pass
                    elif val.path is not None:
                        deps.append(str(val.path))
                elif isinstance(val, (str, Path)):
                    if str(val) != "-":
                        deps.append(str(val))

            if p.name in out_params and p.default is not inspect._empty:
                val = p.default
                if isinstance(val, OutFile):
                    if getattr(val, "is_stream", False):
                        pass
                    elif val.path is not None:
                        outputs.append(str(val.path))
                elif isinstance(val, (str, Path)):
                    if str(val) != "-":
                        outputs.append(str(val))

        register_for_build = bool(outputs)
        logical_name = name or func.__name__

        if prov_path is not None:
            rule_prov_path = prov_path
        else:
            rule_prov_path = str(Path(rule_config.prov_dir) / f"{logical_name}.trig")

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()

            in_files: list[Path] = []
            out_files: list[Path] = []

            for pname in in_params:
                val = bound.arguments.get(pname)
                if isinstance(val, InFile):
                    if val.is_stream or val.path is None:
                        continue
                    in_files.append(val.path)
                elif val is None:
                    continue
                else:
                    if str(val) != "-":
                        in_files.append(Path(val))

            for pname in out_params:
                val = bound.arguments.get(pname)
                if isinstance(val, OutFile):
                    if val.is_stream or val.path is None:
                        continue
                    out_files.append(val.path)
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
            data_graph: rdflib.Graph | None = None
            result = None

            try:
                result = func(*bound.args, **bound.kwargs)
                if isinstance(result, (rdflib.Graph, rdflib.Dataset)):
                    data_graph = result
                return result
            except Exception as e:
                exc = e
                raise
            finally:
                t1 = datetime.now(timezone.utc)
                try:
                    _write_provenance_dataset(
                        base_iri=rule_config.base_iri,
                        name=logical_name,
                        prov_path=rule_prov_path,
                        deps=[str(p) for p in in_files],
                        outputs=[str(p) for p in out_files],
                        t0=t0,
                        t1=t1,
                        data_graph=data_graph,
                        success=exc is None,
                    )
                except Exception as prov_exc:  # noqa: BLE001
                    logging.warning(
                        "Failed to write provenance for %s: %s",
                        logical_name,
                        prov_exc,
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
