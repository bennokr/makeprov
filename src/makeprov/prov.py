from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import platform
import re
import subprocess
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, TypeAlias

from .rdfmixin import RDFMixin
from .config import Frame
from .refs import ArtifactRef


class ProvenanceWriteError(RuntimeError):
    """Raised when a rule's provenance record could not be written.

    Under :class:`~makeprov.config.ProvenanceConfig`'s default ``strict=True``,
    this replaces the historical behavior of silently logging a warning and
    returning a successful result with no provenance on disk.
    """


class UnresolvedArtifactError(ProvenanceWriteError):
    """Raised when a declared output could not be resolved to a real artifact.

    A rule that reports success while one of its declared outputs is missing has
    either failed silently or mis-declared its outputs. Earlier versions dropped
    such artifacts from the graph without comment; that produced provenance
    which looked complete but wasn't.
    """


# ---------- JSON-LD dataclasses ----------

_CONTEXT_PATH = Path(__file__).parent / "context.jsonld"
# Keep a parsed JSON-LD context so it can be embedded directly when requested.
COMMON_CONTEXT = json.loads(_CONTEXT_PATH.read_text(encoding="utf-8"))


JSONLDRef: TypeAlias = str | dict[str, Any]


@dataclass(unsafe_hash=True)
class BaseNode(RDFMixin):
    id: str
    type: Any  # str or tuple[str]
    __context__ = COMMON_CONTEXT


@dataclass(unsafe_hash=True)
class ActivityNode(BaseNode):
    startedAtTime: datetime | None = None
    endedAtTime: datetime | None = None
    wasAssociatedWith: AgentNode | JSONLDRef | None = None
    qualifiedAssociation: AssociationNode | JSONLDRef | None = None
    used: tuple[FileEntity | JSONLDRef] | None = None
    comment: Optional[str] = None


@dataclass(unsafe_hash=True)
class AgentNode(BaseNode):
    label: str | None = None
    hasVersion: str | None = None
    source: str | None = None


@dataclass(unsafe_hash=True)
class PlanNode(BaseNode):
    """The recipe an activity carried out, distinct from whoever ran it.

    PROV separates the plan from the agent executing it. The script at a given
    commit is the plan; the Python runtime and the person invoking it are
    agents. Keeping them apart is what makes the graph mappable onto
    Workflow Run RO-Crate, whose ``instrument`` and ``agent`` are distinct slots.
    """

    label: str | None = None
    hasVersion: str | None = None
    source: str | None = None


@dataclass(unsafe_hash=True)
class PersonNode(BaseNode):
    name: str | None = None
    email: str | None = None


@dataclass(unsafe_hash=True)
class AssociationNode(BaseNode):
    agent: AgentNode | JSONLDRef | None = None
    hadPlan: PlanNode | JSONLDRef | None = None


@dataclass(unsafe_hash=True)
class GraphEntity(BaseNode):
    wasGeneratedBy: ActivityNode | JSONLDRef | None = None
    wasAttributedTo: AgentNode | JSONLDRef | None = None
    generatedAtTime: datetime | None = None


@dataclass(unsafe_hash=True)
class FileEntity(BaseNode):
    format: str | None = None
    extent: int | None = None
    modified: datetime | None = None
    identifier: str | None = None
    wasGeneratedBy: ActivityNode | JSONLDRef | None = None


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
    context: dict = field(default_factory=lambda: deepcopy(COMMON_CONTEXT))

    @classmethod
    def create(
        cls,
        base_iri: str | None,
        name: str,
        run_id: str,
        t0: datetime,
        t1: datetime,
        inputs: list[ArtifactRef],
        outputs: list[ArtifactRef],
        results: list[RDFMixin],
        success: bool = True,
        record_user: bool = False,
    ):
        """Assemble a provenance graph from rule execution details.

        Args:
            base_iri (str | None): Base IRI for generated identifiers.
            name (str): Logical rule name.
            run_id (str): Unique identifier for this run, typically timestamp-based.
            t0 (datetime): Start time of the rule execution.
            t1 (datetime): End time of the rule execution.
            inputs (list[ArtifactRef]): Entities consumed by the rule. Local
                refs are hashed and stat-ed; external refs are cited by IRI.
            outputs (list[ArtifactRef]): Entities produced by the rule.
            results (list[RDFMixin]): Optional result graphs to embed alongside
                provenance records.
            success (bool): Whether the rule completed successfully.
            record_user (bool): Record the invoking user from ``git config`` as
                a ``schema:Person`` agent. Off by default, since provenance
                documents are routinely committed and published.

        Returns:
            Prov: A populated :class:`Prov` instance ready for serialization.

        Examples:
            .. code-block:: python

                prov = Prov.create(
                    base_iri=None,
                    name="uppercase",
                    run_id="20240101T120000-1a2b3c4d",
                    t0=start,
                    t1=end,
                    inputs=[ArtifactRef.local("input.txt")],
                    outputs=[ArtifactRef.local("output.txt")],
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

        context = deepcopy(COMMON_CONTEXT)

        def _apply_context(node: RDFMixin):
            node.__context__ = context
            if hasattr(node, "_build_aliases"):
                node._build_aliases()
            return node

        # Default Github URL heuristic
        if not base_iri and origin and "github.com" in origin:
            # Pin to the commit, not the branch: a branch-based blob URL names
            # different bytes over time, so the same IRI would denote different
            # content on every push.
            revision = commit or _safe_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
            base_iri = origin.replace(".git", "")

            context["@base"] = f"{base_iri}#"
            context["blob"] = f"{base_iri}/blob/{revision}/"

            def _iri(tail: str) -> str:
                return tail # only suffix, put @base in context

            def _file_iri(path: Path) -> str:
                return f'blob:{Path(path).as_posix()}' # blob prefix

        activity_id = _file_iri(f"{script.name}#{name}-{run_id}")
        plan_id = _file_iri(script.name)
        graph_id = _iri(f"graph-{name}")
        assoc_id = f"{activity_id}-association"
        env_id: str | None = None

        activity_used = []

        # Plan: the script at a commit. This is prospective provenance - the
        # recipe - and is deliberately not an agent.
        plan = _apply_context(PlanNode(
            id=plan_id,
            type=("prov:Plan", "prov:Entity", "schema:SoftwareSourceCode"),
            label=script.name,
            hasVersion=commit or None,
            source=origin if origin else None,
        ))

        # Agent: the runtime that actually executed the plan.
        py_version = platform.python_version()
        agent_id = _iri(f"agent-python-{py_version}")
        agent = _apply_context(AgentNode(
            id=agent_id,
            type=("prov:Agent", "prov:SoftwareAgent", "schema:SoftwareApplication"),
            label=f"{platform.python_implementation()} {py_version}",
            hasVersion=py_version,
        ))

        # Agent: the person who ran it, when git can tell us and the caller
        # opted in. This is the slot Workflow Run RO-Crate's `agent` expects
        # (a Person, not software).
        person: PersonNode | None = None
        user_name = _safe_cmd(["git", "config", "--get", "user.name"]) if record_user else None
        user_email = _safe_cmd(["git", "config", "--get", "user.email"]) if record_user else None
        if user_name or user_email:
            person = _apply_context(PersonNode(
                id=f"mailto:{user_email}" if user_email else _iri("agent-user"),
                type=("prov:Agent", "prov:Person", "schema:Person"),
                name=user_name or None,
                email=user_email or None,
            ))

        # Qualified association ties the executing agent to the plan it ran.
        association = _apply_context(AssociationNode(
            id=assoc_id,
            type="prov:Association",
            agent=person.id if person is not None else agent_id,
            hadPlan=plan_id,
        ))

        # Graph entity (metadata entry)
        results_graph = _apply_context(GraphEntity(
            id=graph_id,
            type="prov:Entity",
            wasGeneratedBy=activity_id,
            wasAttributedTo=agent_id,
            generatedAtTime=t1,
        ))

        def _entity_id(ref: ArtifactRef) -> str:
            return ref.id if ref.is_external else _file_iri(ref.path)

        def _entity(ref: ArtifactRef, *, generated_by: str | None = None) -> FileEntity:
            node = FileEntity(
                id=_entity_id(ref),
                type=ref.types if len(ref.types) > 1 else ref.types[0],
                format=ref.media_type,
                extent=ref.extent,
                modified=ref.modified,
                identifier=ref.digest,
                wasGeneratedBy=generated_by,
            )
            if ref.label:
                node._extra = getattr(node, "_extra", {})
                node._extra["label"] = ref.label
            if ref.extra:
                node._extra = getattr(node, "_extra", {})
                node._extra.update(ref.extra)
            return _apply_context(node)

        # Inputs. A missing input still gets an entity node: dropping it would
        # silently delete an edge the caller explicitly declared.
        input_nodes: list[FileEntity] = []
        for ref in inputs:
            if ref.is_external or ref.exists:
                input_nodes.append(_entity(ref.resolve()))
            else:
                logging.warning(
                    "Input %s does not exist; recording it without content metadata",
                    ref.path,
                )
                input_nodes.append(_entity(ref))

        if input_nodes:
            activity_used = input_nodes

        # Outputs. Unlike inputs, a declared output that is missing after a
        # successful run means the rule lied about what it produced.
        output_nodes: list[FileEntity] = []
        for ref in outputs:
            if ref.is_external or ref.exists:
                output_nodes.append(_entity(ref.resolve(), generated_by=activity_id))
            elif success:
                raise UnresolvedArtifactError(
                    f"Rule {name!r} reported success but declared output {ref.path} "
                    "does not exist. Either the rule failed to write it or the "
                    "output declaration is wrong."
                )
            # On failure the output legitimately does not exist; asserting it was
            # generated by the failed activity would be false.

        # Environment + deps
        env_node: EnvNode | None = None
        pname, version, deps_specs = project_metadata()
        if any([pname, version, deps_specs]):
            reqs: list["DepNode"] = []
            normalized_specs: list[str] = []
            for spec in deps_specs:
                spec_str = spec.strip().split(";")[0]
                if not spec_str:
                    continue
                normalized_specs.append(spec_str)
                pkg = spec_str.split()[0]
                pkg_name = re.split(r"[<>=!~ ]", pkg, 1)[0]
                norm = pep503_normalize(pkg_name)
                dep_iri = f"https://pypi.org/project/{norm}/"
                reqs.append(DepNode(id=dep_iri, type="schema:SoftwareSourceCode", label=spec_str))

            env_signature = {
                "name": pname or "",
                "version": version or "",
                "deps": sorted(normalized_specs),
            }
            env_hash = hashlib.sha256(json.dumps(env_signature, sort_keys=True).encode()).hexdigest()[:12]
            env_id = _iri(f"env-{env_hash}")
            env_node = _apply_context(EnvNode(
                id=env_id,
                type=("prov:Entity", "prov:Collection"),
                label="Python environment",
                title=pname or None,
                hasVersion=version or None,
                requires=tuple(reqs) or None,
            ))
            # Link activity -> env via prov:used
            if env_id:
                activity_used.append(env_id)

        # Activity
        activity = _apply_context(ActivityNode(
            id=activity_id,
            type="prov:Activity",
            startedAtTime=t0,
            endedAtTime=t1,
            wasAssociatedWith=(
                (agent_id, person.id) if person is not None else agent_id
            ),
            qualifiedAssociation=assoc_id,
            comment=("task failed" if not success else None),
            used=tuple(activity_used),
        ))

        return cls(
            base_iri=base_iri,
            name=name,
            provenance=[
                activity,
                association,
                plan,
                agent,
                *([person] if person is not None else []),
                *output_nodes,
                *([env_node] if env_node else []),
            ],
            results=[(results_graph, results)] if results is not None else [],
            context=context,
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
        # Descriptor nodes are per-run singletons: every rule in a merged
        # document names the same plan, runtime and user. Emit each once.
        # Activities and entities are deliberately not deduplicated, since the
        # same file can appear with different roles across rules.
        singletons = (EnvNode, PlanNode, AgentNode, PersonNode)
        seen_ids: set[str] = set()
        for prov in provs:
            base_iri = prov.base_iri
            name = prov.name
            for node in prov.provenance:
                if isinstance(node, singletons):
                    if node.id in seen_ids:
                        continue
                    seen_ids.add(node.id)
                all_provenance.append(node)
            all_results.extend(prov.results)
        merged_context = deepcopy(provs[0].context) if provs else deepcopy(COMMON_CONTEXT)
        return cls(base_iri, name, all_provenance, all_results, merged_context)

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
        doc.__context__ = self.context
        doc._build_aliases()
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

        doc = ProvDoc(provenance=tuple(set(self.provenance)))
        doc.__context__ = self.context
        doc._build_aliases()
        for triple in doc.to_graph():
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

    def write(
        self,
        prov_path: str | Path,
        fmt="json",
        frame="provenance",
        context=False,
        context_url: str | None = None,
    ) -> Path:
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
            data = self.to_jsonld(frame=frame, with_context=context)

            if not context and context_url:
                # Point consumers to the published context when not embedding.
                data["@context"] = context_url

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
