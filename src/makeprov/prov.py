from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from .rdfmixin import RDFMixin
from .config import Frame

# ---------- JSON-LD dataclasses ----------

COMMON_CONTEXT = {
    "prov": "http://www.w3.org/ns/prov#",
    "dct": "http://purl.org/dc/terms/",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "schema": "https://schema.org/",
    "id": "@id",
    "type": "@type",
    "provenance": "@graph",
    "startedAtTime": {"@id": "prov:startedAtTime", "@type": "xsd:dateTime"},
    "endedAtTime": {"@id": "prov:endedAtTime", "@type": "xsd:dateTime"},
    "wasAssociatedWith": {
        "@id": "prov:wasAssociatedWith",
        "@type": "@id",
        "@container": "@set",
    },
    "used": {"@id": "prov:used", "@type": "@id", "@container": "@set"},
    "wasGeneratedBy": {"@id": "prov:wasGeneratedBy", "@type": "@id"},
    "wasAttributedTo": {
        "@id": "prov:wasAttributedTo",
        "@type": "@id",
        "@container": "@set",
    },
    "generatedAtTime": {"@id": "prov:generatedAtTime", "@type": "xsd:dateTime"},
    "format": "dct:format",
    "extent": "dct:extent",
    "modified": {"@id": "dct:modified", "@type": "xsd:dateTime"},
    "identifier": "dct:identifier",
    "label": "rdfs:label",
    "title": "dct:title",
    "hasVersion": "dct:hasVersion",
    "source": {"@id": "dct:source", "@type": "@id"},
    "requires": {"@id": "dct:requires", "@type": "@id", "@container": "@set"},
    "comment": "rdfs:comment",
}


@dataclass(unsafe_hash=True)
class BaseNode(RDFMixin):
    id: str
    type: Any  # str or tuple[str]
    __context__ = COMMON_CONTEXT


@dataclass(unsafe_hash=True)
class ActivityNode(BaseNode):
    startedAtTime: datetime | None = None
    endedAtTime: datetime | None = None
    wasAssociatedWith: AgentNode | None = None
    used: tuple[FileEntity] | None = None
    comment: Optional[str] = None


@dataclass(unsafe_hash=True)
class AgentNode(BaseNode):
    label: str | None = None
    hasVersion: str | None = None
    source: str | None = None


@dataclass(unsafe_hash=True)
class GraphEntity(BaseNode):
    wasGeneratedBy: ActivityNode | None = None
    wasAttributedTo: AgentNode | None = None
    generatedAtTime: datetime | None = None


@dataclass(unsafe_hash=True)
class FileEntity(BaseNode):
    format: str | None = None
    extent: int | None = None
    modified: datetime | None = None
    identifier: str | None = None
    wasGeneratedBy: ActivityNode | None = None


@dataclass(unsafe_hash=True)
class EnvNode(BaseNode):
    label: str = "Python environment"
    title: str | None = None
    hasVersion: str | None = None
    requires: tuple[DepNode] | None = None


@dataclass(unsafe_hash=True)
class DepNode(BaseNode):
    label: str | None = None


@dataclass(unsafe_hash=True)
class ProvDoc(RDFMixin):
    provenance: tuple[RDFMixin] = field(default_factory=list)
    __context__ = COMMON_CONTEXT


# ---------- helpers ----------


def _safe_cmd(argv: list[str]) -> str | None:
    """Execute a command safely, returning stdout or ``None`` on failure.

    Args:
        argv (list[str]): Command and arguments to execute.

    Returns:
        str | None: Trimmed stdout from the command, or ``None`` if the command
        fails.

    Examples:
        .. code-block:: python

            commit = _safe_cmd(["git", "rev-parse", "HEAD"])
    """
    try:
        return subprocess.run(
            argv, check=True, capture_output=True, text=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def _caller_script() -> Path:
    """Infer the calling script path for provenance metadata.

    Returns:
        Path: Best-effort absolute path to the executing script or ``unknown``.

    Examples:
        .. code-block:: python

            script_path = _caller_script()
    """
    import sys, inspect

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


def project_metadata(dist_name: str | None = None):
    """Extract package metadata for provenance enrichment.

    Args:
        dist_name (str | None): Distribution name; when ``None`` the caller's
            package name is inferred from the module context.

    Returns:
        tuple[str | None, str | None, list[str]]: Distribution name, version,
        and dependency specifications. Empty values are returned when metadata
        cannot be found.

    Examples:
        .. code-block:: python

            name, version, requires = project_metadata("makeprov")
    """
    import inspect
    import importlib.metadata as im
    from packaging.requirements import Requirement

    if dist_name is None:
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
    
    mandatory = []
    for req_s in (dist.requires or []):
        req = Requirement(req_s)
        # Extras show up as markers containing `extra == ...`
        if req.marker and "extra" in str(req.marker):
            continue
        mandatory.append(str(req))

    name = dist.metadata.get("Name")
    version = dist.version
    return name, version, mandatory


def pep503_normalize(name: str) -> str:
    """Normalize a package name according to PEP 503 rules.

    Args:
        name (str): The distribution name to normalize.

    Returns:
        str: Lowercase, normalized package name with punctuation collapsed.
    """

    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _path_info(path: Path) -> dict[str, Any]:
    """Collect file metadata for provenance entries.

    Args:
        path (Path): File path to inspect.

    Returns:
        dict[str, Any]: Mapping containing format, size, modification time, and
        optional SHA-256 hash when available.

    Examples:
        .. code-block:: python

            details = _path_info(Path("data/output.txt"))
    """
    existed = path.exists()
    info: dict[str, Any] = {
        "format": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "size": path.stat().st_size if existed else 0,
        "modified": datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
        if existed
        else None,
    }
    if existed:
        try:
            info["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001
            pass
    return info


def _base(iri: str | None) -> str:
    """Ensure an IRI ends with a delimiter suitable for concatenation.

    Examples:
        .. code-block:: python

            _base("https://example.com/api")  # "https://example.com/api/"
    """

    if iri is None:
        return ""
    return iri if iri.endswith(("/", "#")) else iri + "/"


# ---------- Public Prov builder ----------
@dataclass
class Prov:
    base_iri: str
    name: str
    provenance: list[RDFMixin]
    results: tuple[GraphEntity, list[RDFMixin]]

    @classmethod
    def create(
        cls,
        base_iri: str | None,
        name: str,
        run_id: str,
        t0: datetime,
        t1: datetime,
        inputs: list[Path],
        outputs: list[Path],
        results: list[RDFMixin],
        success: bool = True,
    ):
        """Assemble a provenance graph from rule execution details.

        Args:
            base_iri (str | None): Base IRI for generated identifiers.
            name (str): Logical rule name.
            run_id (str): Unique identifier for this run, typically timestamp-based.
            t0 (datetime): Start time of the rule execution.
            t1 (datetime): End time of the rule execution.
            inputs (list[Path]): Input files consumed by the rule.
            outputs (list[Path]): Output files produced by the rule.
            results (list[RDFMixin]): Optional result graphs to embed alongside
                provenance records.
            success (bool): Whether the rule completed successfully.

        Returns:
            Prov: A populated :class:`Prov` instance ready for serialization.

        Examples:
            .. code-block:: python

                prov = Prov.create(
                    base_iri=None,
                    name="uppercase",
                    run_id="20240101T120000",
                    t0=start,
                    t1=end,
                    inputs=[Path("input.txt")],
                    outputs=[Path("output.txt")],
                    results=[],
                )
        """        
        def _iri(tail: str) -> str:
            return f"{_base(base_iri)}{tail}"

        def _file_iri(path: Path | str) -> str:
            return _iri(Path(path).as_posix())

        script = _caller_script()
        commit = _safe_cmd(["git", "rev-parse", "HEAD"])
        origin = _safe_cmd(["git", "config", "--get", "remote.origin.url"])

        # Default Github URL heuristic
        if not base_iri and origin and "github.com" in origin:
            branch = _safe_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
            base_iri = origin.replace(".git", "")

            COMMON_CONTEXT["@base"] = f"{base_iri}#"
            COMMON_CONTEXT["blob"] = f"{base_iri}/blob/{branch}/"

            def _iri(tail: str) -> str:
                return tail # only suffix, put @base in context

            def _file_iri(path: Path) -> str:
                return f'blob:{Path(path).as_posix()}' # blob prefix

        activity_id = _file_iri(f"{script.name}#{name}-{run_id}")
        agent_id = _file_iri(script.name)
        graph_id = _iri(f"graph-{name}")
        env_id = _iri(f"env-{run_id}")

        activity_used = []

        # Agent
        agent = AgentNode(
            id=agent_id,
            type=("prov:Agent", "prov:SoftwareAgent", "schema:SoftwareSourceCode"),
            label=script.name,
            hasVersion=commit or None,
            source=origin if origin else None,
        )

        # Graph entity (metadata entry)
        results_graph = GraphEntity(
            id=graph_id,
            type="prov:Entity",
            wasGeneratedBy=activity_id,
            wasAttributedTo=agent_id,
            generatedAtTime=t1,
        )

        # Inputs
        input_nodes: list[FileEntity] = []
        for p in inputs:
            if not p.exists():
                continue
            info = _path_info(p)
            fid = _file_iri(p)
            input_nodes.append(
                FileEntity(
                    id=fid,
                    type="prov:Entity",
                    format=info["format"],
                    extent=info["size"],
                    modified=info["modified"] if info.get("modified") else None,
                    identifier=f"sha256:{info['sha256']}"
                    if info.get("sha256")
                    else None,
                )
            )

        if input_nodes:
            activity_used = input_nodes

        # Outputs
        output_nodes: list[FileEntity] = []
        for p in outputs:
            if not p.exists():
                continue
            info = _path_info(p)
            oid = _file_iri(p)
            output_nodes.append(
                FileEntity(
                    id=oid,
                    type="prov:Entity",
                    format=info["format"],
                    extent=info["size"],
                    modified=info["modified"] if info.get("modified") else None,
                    wasGeneratedBy=activity_id,
                )
            )

        # Environment + deps
        env_node: EnvNode | None = None
        pname, version, deps_specs = project_metadata()
        if any([pname, version, deps_specs]):
            reqs: list["DepNode"] = []
            for spec in deps_specs:
                spec_str = spec.strip().split(";")[0]
                if not spec_str:
                    continue
                pkg = spec_str.split()[0]
                pkg_name = re.split(r"[<>=!~ ]", pkg, 1)[0]
                norm = pep503_normalize(pkg_name)
                dep_iri = f"https://pypi.org/project/{norm}/"
                reqs.append(DepNode(id=dep_iri, type="schema:SoftwareSourceCode", label=spec_str))
            env_node = EnvNode(
                id=env_id,
                type=("prov:Entity", "prov:Collection"),
                label="Python environment",
                title=pname or None,
                hasVersion=version or None,
                requires=tuple(reqs) or None,
            )
            # Link activity -> env via prov:used
            activity_used.append(env_id)

        # Activity
        activity = ActivityNode(
            id=activity_id,
            type="prov:Activity",
            startedAtTime=t0,
            endedAtTime=t1,
            wasAssociatedWith=agent_id,
            comment=("task failed" if not success else None),
            used=tuple(activity_used),
        )

        return cls(
            base_iri=base_iri,
            name=name,
            provenance=[
                activity,
                agent,
                *output_nodes,
                *([env_node] if env_node else []),
            ],
            results=[(results_graph, results)] if results is not None else [],
        )

    @classmethod
    def merge(cls, provs: list["Prov"]) -> "Prov":
        """Combine multiple provenance documents into one.

        Args:
            provs (list[Prov]): Provenance objects to merge.

        Returns:
            Prov: A new object containing combined provenance and results from
            all inputs.

        Examples:
            .. code-block:: python

                merged = Prov.merge([prov_a, prov_b])
        """
        base_iri, name, all_provenance, all_results = None, None, [], []
        for prov in provs:
            base_iri = prov.base_iri
            name = prov.name
            all_provenance.extend(prov.provenance)
            all_results.extend(prov.results)
        return cls(base_iri, name, all_provenance, all_results)

    def _iri(self, tail: str) -> str:
        return f"{_base(self.base_iri)}{tail}"

    def _result_entries(self, with_context: bool) -> list[dict]:
        entries: list[dict] = []
        for result_graph, results in self.results:
            if results:
                graph = []
                if any(isinstance(result, RDFMixin) for result in results):
                    for result in results:
                        if result is not None and isinstance(result, RDFMixin):
                            o = result.to_jsonld(with_context=with_context)
                            graph.append(o)
                else:
                    try:
                        import rdflib
                        for result in results:
                            if isinstance(result, (rdflib.Graph, rdflib.Dataset)):
                                o = json.loads(result.serialize(format="json-ld"))
                                graph.extend(o)
                    except:
                        pass
                if graph:
                    results_obj = result_graph.to_jsonld(with_context=False)
                    results_obj.setdefault("@graph", []).extend(graph)
                    entries.append(results_obj)
        return entries

    def to_jsonld(self, frame: Frame = "provenance", with_context: bool = False) -> dict:
        doc = ProvDoc(provenance=tuple(set(self.provenance)))
        data = doc.to_jsonld(with_context=with_context)

        provenance_entries = list(data.pop("provenance", []))
        context_obj = data.pop("@context", None) if with_context else None

        if frame == "provenance":
            if with_context and context_obj is not None:
                data["@context"] = context_obj
            data["provenance"] = provenance_entries + self._result_entries(with_context)
            return data

        # frame == "results": provenance is nested with its own graph identifier
        prov_id = self._iri(f"prov-{self.name}")
        if with_context:
            updated_context = dict(context_obj or {})
            updated_context["provenance"] = {
                "@id": "prov:has_provenance",
                "@container": ["@graph", "@id"],
            }
            data["@context"] = updated_context

        data["@graph"] = self._result_entries(with_context)
        data["provenance"] = {prov_id: provenance_entries}
        return data

    def to_graph(self, frame: Frame = "provenance"):
        try:
            import rdflib
        except ImportError as exc:
            raise RuntimeError("rdflib is required for Prov.to_graph()") from exc

        ds = rdflib.Dataset()
        ds.bind("", self.base_iri)

        default_graph = ds.default_context
        prov_graph_target = default_graph
        if frame == "results":
            prov_graph_target = ds.get_context(self._iri(f"prov-{self.name}"))

        for triple in ProvDoc(provenance=tuple(set(self.provenance))).to_graph():
            prov_graph_target.add(triple)

        for result_graph, results in self.results:
            if any(r is not None for r in results):
                for triple in result_graph.to_graph():
                    prov_graph_target.add(triple)
            gx = ds.get_context(result_graph.id)
            for result in results:
                if result is not None:
                    if isinstance(result, (rdflib.Graph, rdflib.Dataset)):
                        for triple in result:
                            gx.add(triple)
                    elif hasattr(result, "to_graph"):
                        for triple in result.to_graph():
                            gx.add(triple)

        return ds

    def write(self, prov_path: str | Path, fmt="json", frame="provenance", context=False) -> Path:
        """Serialize provenance to disk.

        Args:
            prov_path (str | Path): Output path (without extension) where the
                provenance document should be written.
            fmt (str): Output format, ``"json"`` for JSON-LD or ``"trig"`` for
                RDF TriG.
            frame (str): Which structure to make primary subject of jsonld or 
                trig named graph. Options: `"provenance"` or `"results"`.
            context (bool): Whether to include the JSON-LD context inline when
                writing JSON.

        Returns:
            Path: The path to the written provenance document with extension.

        Raises:
            Exception: If the requested format is unsupported.

        Examples:
            .. code-block:: python

                output = prov.write("prov/uppercase", fmt="json", context=True)
        """
        out = Path(prov_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        if fmt == "json":
            print(context)
            data = self.to_jsonld(frame=frame, with_context=context)
            final = out.with_suffix(".json")
            logging.info("Writing JSON-LD provenance %s", final)
            final.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return final

        elif fmt == "trig":
            ds = self.to_graph(frame=frame)
            final = out.with_suffix(".trig")
            logging.info("Writing TRIG provenance %s", final)
            ds.serialize(final, format="trig")
            return final

        else:
            raise Exception(f"No handler to write Prov object in format '{fmt}'")
