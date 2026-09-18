"""Convert ``reprounzip graph --json`` output to makeprov's PROV model.

This module reads an *existing* ReproZip graph; it does not run a tracer or
require ReproZip to be installed. The graph contains observed file accesses,
not semantic dependencies, file contents, or network I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .config import ProvenanceConfig
from .prov import (
    COMMON_CONTEXT,
    ActivityNode,
    AgentNode,
    AssociationNode,
    FileEntity,
    PlanNode,
    Prov,
    apply_context,
    resolve_iris,
)


def _objects(value: Any, where: str) -> list[dict[str, Any]]:
    """Require an array of objects instead of silently discarding malformed data."""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{where} must be an array of objects")
    return value


def _paths(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(p, str) or not p for p in value):
        raise ValueError(f"{where} must be an array of nonempty file paths")
    return tuple(dict.fromkeys(value))


def _timestamp(process: Mapping[str, Any], activity: ActivityNode) -> None:
    """Keep raw numeric trace times without assuming an epoch or unit."""
    value = process.get("start_time")
    if value is None:
        return
    if isinstance(value, str):
        try:
            activity.startedAtTime = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return
        except ValueError:
            pass
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError("process.start_time must be a string or number")
    activity._extra.setdefault("schema:additionalProperty", []).append(
        {"@type": "schema:PropertyValue", "schema:name": "ReproZip raw start_time", "schema:value": value}
    )


def build_prov_from_reprozip(
    graph: Mapping[str, Any],
    *,
    config: ProvenanceConfig | None = None,
    name: str = "reprozip",
) -> Prov:
    """Translate the documented ``reprounzip graph --json`` structure.

    Process references in ``parent`` are run-local *indices*, not PIDs. A
    pathname may have multiple writers, so separate per-writer output entities
    are used when a single pathname cannot identify one generated file state.
    """
    if not isinstance(graph, Mapping):
        raise ValueError("ReproZip graph must be a JSON object")
    runs = _objects(graph.get("runs"), "runs")
    packages = _objects(graph.get("packages"), "packages")
    other_files = _paths(graph.get("other_files"), "other_files")
    inputs_outputs = _objects(graph.get("inputs_outputs"), "inputs_outputs")
    config = config or ProvenanceConfig()
    context = deepcopy(COMMON_CONTEXT)
    context["wasInformedBy"] = {"@id": "prov:wasInformedBy", "@type": "@id", "@container": "@set"}
    context["isPartOf"] = {"@id": "dct:isPartOf", "@type": "@id"}
    minter = resolve_iris(config.base_iri, context, forge_profiles=config.forge_profiles)
    # Distinguishes separate traces without inventing an unregistered URN NID.
    digest = hashlib.sha256(json.dumps(graph, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    prefix = f"reprozip/trace/{digest}/"

    def trace_id(tail: str) -> str:
        return minter.mint(prefix + tail)

    def file_id(path: str) -> str:
        p = Path(path)
        # An observed absolute path is a filesystem location, not a Git blob.
        return p.as_uri() if p.is_absolute() else minter.file(quote(path, safe="/"))

    process_runs: list[list[dict[str, Any]]] = []
    all_paths = set(other_files)
    writers: Counter[str] = Counter()
    read_written_same_activity: set[str] = set()
    for ri, run in enumerate(runs):
        processes = _objects(run.get("processes"), f"runs[{ri}].processes")
        process_runs.append(processes)
        for pi, proc in enumerate(processes):
            reads = _paths(proc.get("reads"), f"runs[{ri}].processes[{pi}].reads")
            writes = _paths(proc.get("writes"), f"runs[{ri}].processes[{pi}].writes")
            all_paths.update(reads)
            all_paths.update(writes)
            writers.update(writes)
            read_written_same_activity.update(set(reads) & set(writes))

    # ReproZip's input/output manifest is metadata, not independent evidence of
    # an access. It cannot reintroduce paths removed by graph filtering.
    io_names: dict[str, str] = {}
    for i, item in enumerate(inputs_outputs):
        path = item.get("path")
        label = item.get("name")
        if not isinstance(path, str) or not isinstance(label, str):
            raise ValueError(f"inputs_outputs[{i}] requires string path and name")
        io_names[path] = label

    file_packages: dict[str, str] = {}
    package_nodes: list[FileEntity] = []
    for i, pkg in enumerate(packages):
        pkg_name = pkg.get("name")
        if not isinstance(pkg_name, str) or not pkg_name:
            raise ValueError(f"packages[{i}].name must be a nonempty string")
        pkg_files = _paths(pkg.get("files"), f"packages[{i}].files")
        pkg_id = trace_id(f"package/{i}-{quote(pkg_name, safe='')}")
        package = FileEntity(id=pkg_id, type="prov:Entity")
        package._extra["label"] = pkg_name
        if pkg.get("version") is not None:
            package._extra["hasVersion"] = str(pkg["version"])
        package_nodes.append(package)
        for path in pkg_files:
            if path in file_packages and file_packages[path] != pkg_id:
                raise ValueError(f"File belongs to multiple ReproZip packages: {path}")
            file_packages[path] = pkg_id
            all_paths.add(path)

    file_nodes: dict[str, FileEntity] = {}
    for path in sorted(all_paths):
        entity = FileEntity(id=file_id(path), type="prov:Entity")
        entity._extra["label"] = path
        if path in io_names:
            entity._extra["schema:alternateName"] = io_names[path]
        if path in file_packages:
            entity._extra["isPartOf"] = file_packages[path]
        file_nodes[path] = entity

    nodes: list[Any] = [*package_nodes, *file_nodes.values()]
    # ReproZip prioritizes writes when an open has both READ and WRITE flags.
    # A process listed as a writer therefore cannot be assumed to have read it.
    split_outputs = {p for p, count in writers.items() if count > 1} | read_written_same_activity
    for ri, (run, processes) in enumerate(zip(runs, process_runs)):
        run_name = run.get("name", f"run {ri}")
        if not isinstance(run_name, str):
            raise ValueError(f"runs[{ri}].name must be a string")
        for pi, proc in enumerate(processes):
            activity_id = trace_id(f"run/{ri}/process/{pi}")
            argv = proc.get("argv")
            if argv is not None and (not isinstance(argv, list) or any(not isinstance(a, str) for a in argv)):
                raise ValueError(f"runs[{ri}].processes[{pi}].argv must be an array of strings or null")
            parent = proc.get("parent")
            if parent is not None:
                if (not isinstance(parent, list) or len(parent) != 2
                        or type(parent[0]) is not int or not 0 <= parent[0] < pi
                        or not isinstance(parent[1], str)):
                    raise ValueError(f"runs[{ri}].processes[{pi}].parent must reference an earlier process index")

            reads = _paths(proc["reads"], f"runs[{ri}].processes[{pi}].reads")
            writes = _paths(proc["writes"], f"runs[{ri}].processes[{pi}].writes")
            output_ids: list[str] = []
            for path in writes:
                if path in split_outputs:
                    output_id = activity_id + "/output/" + hashlib.sha256(path.encode()).hexdigest()[:16]
                    output = FileEntity(id=output_id, type="prov:Entity", wasGeneratedBy=activity_id)
                    output._extra["label"] = path
                    output._extra["source"] = file_id(path)
                    if path in file_packages:
                        output._extra["isPartOf"] = file_packages[path]
                    nodes.append(output)
                else:
                    output_id = file_id(path)
                    file_nodes[path].wasGeneratedBy = activity_id
                output_ids.append(output_id)

            label = proc.get("long_name") or proc.get("name") or f"process {pi}"
            if not isinstance(label, str):
                raise ValueError(f"runs[{ri}].processes[{pi}].name must be a string")
            activity = ActivityNode(
                id=activity_id,
                type="prov:Activity",
                used=tuple(file_id(p) for p in reads) or None,
                generated=tuple(output_ids) or None,
                comment="File access observed by ReproZip; not a declared semantic dependency.",
            )
            activity._extra["label"] = f"{run_name}: {label}"
            _timestamp(proc, activity)
            if parent is not None:
                activity._extra["wasInformedBy"] = [trace_id(f"run/{ri}/process/{parent[0]}")]
                activity._extra["schema:additionalProperty"] = [
                    *activity._extra.get("schema:additionalProperty", []),
                    {"@type": "schema:PropertyValue", "schema:name": "ReproZip parent event", "schema:value": parent[1]},
                ]

            # The process image, not ReproZip's tracer, is the executing agent.
            description = proc.get("description")
            executable = description.split("\n", 1)[0] if isinstance(description, str) else ""
            if executable in ("None", "-"):
                executable = ""
            agent_id = activity_id + "/agent"
            agent = AgentNode(
                id=agent_id,
                type=("prov:Agent", "prov:SoftwareAgent", "schema:SoftwareApplication"),
                label=executable or label,
                source=file_id(executable) if executable.startswith("/") else None,
            )
            activity.wasAssociatedWith = agent_id
            nodes.append(agent)
            if argv:
                plan_id = activity_id + "/plan"
                plan = PlanNode(
                    id=plan_id,
                    type=("prov:Plan", "prov:Entity"),
                    label=shlex.join(argv),
                    source=file_id(executable) if executable.startswith("/") else None,
                )
                assoc = AssociationNode(
                    id=activity_id + "/association",
                    type="prov:Association",
                    agent=agent_id,
                    hadPlan=plan_id,
                )
                activity.qualifiedAssociation = assoc.id
                nodes.extend((plan, assoc))
            nodes.append(activity)

    return Prov(
        base_iri=minter.base_iri or "",
        name=name,
        provenance=[apply_context(node, context) for node in nodes],
        results=[],
        context=context,
    )


def convert_reprozip_graph(
    path: str | Path, *, config: ProvenanceConfig | None = None, name: str = "reprozip"
) -> Prov:
    """Read a ReproZip JSON graph from disk and construct a :class:`Prov`."""
    with Path(path).open(encoding="utf-8") as stream:
        return build_prov_from_reprozip(json.load(stream), config=config, name=name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="makeprov-reprozip",
        description="Convert an existing 'reprounzip graph --json' file to PROV/JSON-LD or TriG.",
    )
    parser.add_argument("graph", type=Path, help="Existing ReproZip graph JSON file")
    parser.add_argument("--output", type=Path, help="Output path without an extension")
    parser.add_argument("--base-iri", help="Base IRI for generated provenance identifiers")
    parser.add_argument("--name", default="reprozip", help="Provenance document name")
    parser.add_argument("--format", choices=("json", "trig"), default="json")
    args = parser.parse_args(argv)
    prov = convert_reprozip_graph(
        args.graph, config=ProvenanceConfig(base_iri=args.base_iri), name=args.name
    )
    # Include the modified context: the published makeprov context does not
    # define this converter's wasInformedBy/isPartOf aliases.
    prov.write(args.output or args.graph.with_suffix(""), fmt=args.format, context=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
