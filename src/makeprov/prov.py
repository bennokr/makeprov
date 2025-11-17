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
from typing import Any, Optional

from .jsonld import JSONLDMixin

# ---------- JSON-LD dataclasses ----------

COMMON_CONTEXT = {
    "prov": "http://www.w3.org/ns/prov#",
    "dct": "http://purl.org/dc/terms/",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "id": "@id",
    "type": "@type",
    "provenance": "@graph",
    "startedAtTime": { "@id": "prov:startedAtTime", "@type": "xsd:dateTime"},
    "endedAtTime": { "@id": "prov:endedAtTime", "@type": "xsd:dateTime"},
    "wasAssociatedWith": {"@id": "prov:wasAssociatedWith", "@type": "@id", "@container": "@set"},
    "used": {"@id": "prov:used", "@type": "@id", "@container": "@set"},
    "wasGeneratedBy": {"@id": "prov:wasGeneratedBy", "@type": "@id"},
    "wasAttributedTo": {"@id": "prov:wasAttributedTo", "@type": "@id", "@container": "@set"},
    "generatedAtTime": { "@id": "prov:generatedAtTime", "@type": "xsd:dateTime"},
    "format": "dct:format",
    "extent": "dct:extent",
    "modified": { "@id": "dct:modified", "@type": "xsd:dateTime"},
    "identifier": "dct:identifier",
    "label": "rdfs:label",
    "title": "dct:title",
    "hasVersion": "dct:hasVersion",
    "source": {"@id": "dct:source", "@type": "@id"},
    "requires": {"@id": "dct:requires", "@type": "@id", "@container": "@set"},
    "comment": "rdfs:comment",
}

@dataclass
class BaseNode(JSONLDMixin):
    id: str
    type: Any  # str or list[str]
    __context__ = COMMON_CONTEXT

@dataclass
class ActivityNode(BaseNode):
    startedAtTime: datetime | None = None
    endedAtTime: datetime | None = None
    wasAssociatedWith: AgentNode | None = None
    used: list[FileEntity] | None = None
    comment: Optional[str] = None

@dataclass
class AgentNode(BaseNode):
    label: str | None = None
    hasVersion: str | None = None
    source: str | None = None

@dataclass
class GraphEntity(BaseNode):
    wasGeneratedBy: ActivityNode | None = None
    wasAttributedTo: AgentNode | None = None
    generatedAtTime: datetime | None = None

@dataclass
class FileEntity(BaseNode):
    format: str | None = None
    extent: int | None = None
    modified: datetime | None = None
    identifier: str | None = None
    wasGeneratedBy: ActivityNode | None = None

@dataclass
class EnvNode(BaseNode):
    label: str = "Python environment"
    title: str | None = None
    hasVersion: str | None = None
    requires: list[DepNode] | None = None

@dataclass
class DepNode(BaseNode):
    label: str | None = None

@dataclass
class ProvDoc(JSONLDMixin):
    provenance: list[Any] = field(default_factory=list)
    __context__ = COMMON_CONTEXT


@dataclass
class ProvResult:
    prov: "Prov"
    result: Any | None = None

# ---------- helpers ----------

def _safe_cmd(argv: list[str]) -> str | None:
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return None

def _caller_script() -> Path:
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
    import inspect
    import importlib.metadata as im

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

    name = dist.metadata.get("Name")
    version = dist.version
    requires = dist.requires or []
    return name, version, requires

def pep503_normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())

def _path_info(path: Path) -> dict[str, Any]:
    existed = path.exists()
    info: dict[str, Any] = {
        "format": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "size": path.stat().st_size if existed else 0,
        "modified": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        if existed else None,
    }
    if existed:
        try:
            info["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001
            pass
    return info

def _base(iri: str) -> str:
    return iri if iri.endswith(("/", "#")) else iri + "/"


# ---------- Public Prov builder ----------

class Prov:

    def __init__(self,
        base_iri: str,
        name: str,
        run_id: str,
        t0: datetime,
        t1: datetime,
        inputs: list[Path],
        outputs: list[Path],
        success: bool = True,
    ):
        self.base_iri = base_iri
        
        def _iri(kind: str, tail: str) -> str:
            return f"{_base(base_iri)}{kind}/{tail}"
        
        def _file_iri(kind: str, path: Path) -> str:
            return _iri(kind, path.as_posix())

        script = _caller_script()
        commit = _safe_cmd(["git", "rev-parse", "HEAD"])
        origin = _safe_cmd(["git", "config", "--get", "remote.origin.url"])

        activity_id = _iri("run", f"{name}/{run_id}")
        agent_id = _iri("agent", script.name)
        self.graph_id = _iri("graph", name)
        env_id = _iri("env", run_id)

        # Activity
        self.activity = ActivityNode(
            id=activity_id,
            type="prov:Activity",
            startedAtTime=t0,
            endedAtTime=t1,
            wasAssociatedWith=agent_id,
            comment=("task failed" if not success else None),
        )

        # Agent
        self.agent = AgentNode(
            id=agent_id,
            type=["prov:Agent", "prov:SoftwareAgent"],
            label=script.name,
            hasVersion=commit or None,
            source=origin if origin else None,
        )

        # Graph entity (metadata entry)
        self.graph_meta = GraphEntity(
            id=self.graph_id,
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
            fid = _file_iri("src", p)
            input_nodes.append(
                FileEntity(
                    id=fid,
                    type="prov:Entity",
                    format=info["format"],
                    extent=info["size"],
                    modified=info["modified"] if info.get("modified") else None,
                    identifier=f"sha256:{info['sha256']}" if info.get("sha256") else None,
                )
            )

        if input_nodes:
            self.activity.used = input_nodes

        # Outputs
        self.output_nodes: list[FileEntity] = []
        for p in outputs:
            if not p.exists():
                continue
            info = _path_info(p)
            oid = _file_iri("out", p)
            self.output_nodes.append(
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
        self.env_node: EnvNode | None = None
        pname, version, deps_specs = project_metadata()
        if any([pname, version, deps_specs]):
            reqs: list[dict] = []
            for spec in deps_specs:
                spec_str = spec.strip().split(';')[0]
                if not spec_str:
                    continue
                pkg = spec_str.split()[0]
                pkg_name = re.split(r"[<>=!~ ]", pkg, 1)[0]
                norm = pep503_normalize(pkg_name)
                dep_iri = f"https://pypi.org/project/{norm}/"
                reqs.append(DepNode(id=dep_iri, type="rdfs:Resource", label=spec_str))
            self.env_node = EnvNode(
                id=env_id,
                type=["prov:Entity", "prov:Collection"],
                label="Python environment",
                title=pname or None,
                hasVersion=version or None,
                requires=reqs or None,
            )
            # Link activity -> env via prov:used
            if self.activity.used is None:
                self.activity.used = []
            self.activity.used.append(env_id)

    def to_doc(self, *, include_graph_meta: bool = False) -> ProvDoc:
        provenance: list[Any] = [
            self.activity,
            self.agent,
            *self.output_nodes,
            *([self.env_node] if self.env_node else []),
        ]

        if include_graph_meta:
            provenance.append(self.graph_meta)

        return ProvDoc(provenance=provenance)

    def to_dataset(self, result=None):
        import rdflib

        ds = rdflib.Dataset()
        ds.bind("", self.base_iri)
        default_graph = ds.default_context

        for triple in self.to_doc(include_graph_meta=True).to_graph():
            default_graph.add(triple)

        if result is not None and isinstance(result, (rdflib.Graph, rdflib.Dataset)):
            gx = ds.get_context(self.graph_id)
            for triple in result:
                gx.add(triple)

        return ds

    def write(self, prov_path: str | Path, result=None, fmt="json", jsonld_with_context=False) -> Path:
        out = Path(prov_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            data = self.to_doc().to_jsonld(with_context=jsonld_with_context)
            if result is not None and isinstance(result, JSONLDMixin):
                data["result"] = [result.to_jsonld(with_context=jsonld_with_context)]
            final = out.with_suffix(".json")
            logging.info("Writing JSON-LD provenance %s", final)
            final.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return final
        elif fmt == "trig":
            ds = self.to_dataset(result=result)

            final = out.with_suffix(".trig")
            logging.info("Writing TRIG provenance %s", final)
            ds.serialize(final, format="trig")
            return final

        else:
            raise Exception(f"No handler to write Prov object in format '{fmt}'")


def write_combined_prov(
    provs: list[ProvResult],
    prov_path: str | Path,
    fmt: str = "json",
    jsonld_with_context: bool = False,
):
    if not provs:
        raise ValueError("No provenance objects provided for combination")

    out = Path(prov_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        combined_doc = ProvDoc(provenance=[])
        for prov_result in provs:
            combined_doc.provenance.extend(prov_result.prov.to_doc().provenance)

        data = combined_doc.to_jsonld(with_context=jsonld_with_context)
        data["result"] = []

        for prov_result in provs:
            if isinstance(prov_result.result, JSONLDMixin):
                data["result"].append(
                    prov_result.result.to_jsonld(with_context=jsonld_with_context)
                )

        final = out.with_suffix(".json")
        logging.info("Writing combined JSON-LD provenance %s", final)
        final.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return final

    if fmt == "trig":
        import rdflib

        ds = rdflib.Dataset()

        for prov_result in provs:
            ds.bind("", prov_result.prov.base_iri)
            default_graph = ds.default_context

            for triple in prov_result.prov.to_doc(include_graph_meta=True).to_graph():
                default_graph.add(triple)

            if isinstance(prov_result.result, (rdflib.Graph, rdflib.Dataset)):
                gx = ds.get_context(prov_result.prov.graph_id)
                for triple in prov_result.result:
                    gx.add(triple)

        final = out.with_suffix(".trig")
        logging.info("Writing combined TRIG provenance %s", final)
        ds.serialize(final, format="trig")
        return final

    raise Exception(f"No handler to write combined Prov objects in format '{fmt}'")