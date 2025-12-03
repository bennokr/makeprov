from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import GLOBAL_CONFIG, ProvenanceConfig, apply_config
from .prov import (
    COMMON_CONTEXT,
    ActivityNode,
    AgentNode,
    FileEntity,
    Prov,
    _path_info,
    _safe_cmd,
)

# Ensure prov:wasInformedBy is present for job dependency edges.
COMMON_CONTEXT.setdefault(
    "wasInformedBy",
    {"@id": "prov:wasInformedBy", "@type": "@id", "@container": "@set"},
)


def _run(cmd: list[str]) -> str:
    """Run a command and return combined stdout and stderr."""
    proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return (proc.stdout or "") + (proc.stderr or "")


def _extract_json_blob(text: str) -> Any:
    """Extract a JSON object from Snakemake output that may contain log noise."""
    stripped = text.strip()
    match = re.search(r"\{.*\}\s*$", stripped, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))

    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Could not locate JSON payload in snakemake output.")
    return json.loads(stripped[start : end + 1])


def get_d3dag_json(
    snakemake_exe: str,
    snakemake_args: list[str],
    *,
    forceall_dag: bool,
) -> dict[str, Any]:
    """Call ``snakemake --d3dag`` and decode the resulting JSON."""
    cmd = [snakemake_exe, *snakemake_args]
    if forceall_dag:
        cmd.append("--forceall")
    cmd += ["--d3dag", "-n"]
    return _extract_json_blob(_run(cmd))


def get_detailed_summary(
    snakemake_exe: str,
    snakemake_args: list[str],
) -> list[dict[str, str]]:
    """Call ``snakemake --detailed-summary`` and parse its tabular output."""
    cmd = [snakemake_exe, *snakemake_args, "--detailed-summary"]
    out = _run(cmd)

    lines = [
        line
        for line in out.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return []

    header_line = lines[0]
    delimiter = "\t" if "\t" in header_line else None
    header = (
        header_line.split(delimiter)
        if delimiter
        else header_line.split()
    )

    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        parts = line.split(delimiter) if delimiter else line.split()
        if len(parts) < len(header):
            parts += [""] * (len(header) - len(parts))
        rows.append(dict(zip(header, parts)))

    return rows


def _get_any(row: dict[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = row.get(key, "")
        if value != "":
            return value
    return default


def _split_files(cell: str) -> list[str]:
    cell = (cell or "").strip()
    if not cell or cell == "-":
        return []
    parts = re.split(r"[,\s]+", cell)
    return [part for part in (p.strip() for p in parts) if part and part != "-"]


@dataclass(eq=False)
class _JobGroup:
    rule: str
    version: str
    inputs: tuple[str, ...]
    shellcmd: str
    status: str
    plan: str
    outputs: list[str]
    jobid: int | None = None


def _group_summary_rows(summary_rows: list[dict[str, str]]) -> list[_JobGroup]:
    groups: dict[tuple, _JobGroup] = {}

    for row in summary_rows:
        output_file = _get_any(row, "output_file", "filename")
        rule = _get_any(
            row,
            "rule",
            default=_get_any(row, "rule version", default="-"),
        )
        version = _get_any(row, "version", default="-")

        if "rule version" in row and "rule" not in row and "version" not in row:
            rule_version = (row.get("rule version") or "").strip()
            if rule_version:
                version = rule_version

        inputs = tuple(sorted(_split_files(_get_any(row, "input-file(s)", "input file(s)"))))
        shellcmd = _get_any(row, "shellcmd", "shell command", default="-")
        status = _get_any(row, "status", default="-")
        plan = _get_any(row, "plan", default="-")

        key = (rule, version, inputs, shellcmd, status, plan)
        if key not in groups:
            groups[key] = _JobGroup(
                rule=rule,
                version=version,
                inputs=inputs,
                shellcmd=shellcmd,
                status=status,
                plan=plan,
                outputs=[],
            )
        if output_file:
            groups[key].outputs.append(output_file)

    return sorted(
        groups.values(),
        key=lambda group: (group.rule, sorted(group.outputs)[:1], group.shellcmd),
    )


def _index_d3dag(dag: dict[str, Any]) -> tuple[dict[int, str], list[tuple[int, int]]]:
    nodes = dag.get("nodes") or []
    edges = dag.get("links") or dag.get("edges") or []

    jobid_to_rule: dict[int, str] = {}
    for node in nodes:
        meta = node.get("value", {})
        if not isinstance(meta, dict):
            meta = {}
        node_id = node.get("id", meta.get("jobid"))
        rule = meta.get("rule") or meta.get("label") or node.get("name") or ""
        if node_id is None:
            continue
        jobid_to_rule[int(node_id)] = str(rule)

    edge_list: list[tuple[int, int]] = []
    for edge in edges:
        upstream = edge.get("u", edge.get("source"))
        downstream = edge.get("v", edge.get("target"))
        if upstream is None or downstream is None:
            continue
        edge_list.append((int(upstream), int(downstream)))

    return jobid_to_rule, edge_list


def build_prov_from_snakemake(
    dag: dict[str, Any],
    summary_rows: list[dict[str, str]],
    *,
    config: ProvenanceConfig,
    name: str = "snakemake",
) -> Prov:
    base = config.base_iri or "urn:snakemake:"
    if not base.endswith(("/", "#", ":")):
        base += "/"

    def file_id(path_str: str) -> str:
        return f"{base}file/{Path(path_str).as_posix()}"

    def job_fallback_id(group: _JobGroup) -> str:
        digest = hashlib.sha1(  # noqa: S324
            (
                "|".join(
                    [
                        group.rule,
                        group.version,
                        ",".join(group.inputs),
                        ",".join(sorted(group.outputs)),
                        group.shellcmd,
                    ]
                )
            ).encode("utf-8")
        ).hexdigest()[:12]
        return f"{base}job/{group.rule}/{digest}"

    smk_version = _safe_cmd(["snakemake", "--version"])
    agent = AgentNode(
        id=f"{base}agent/snakemake",
        type=("prov:Agent", "prov:SoftwareAgent"),
        label="snakemake",
        hasVersion=smk_version or None,
        source=None,
    )

    jobid_to_rule, d3_edges = _index_d3dag(dag)
    jobids_by_rule: dict[str, list[int]] = {}
    for jobid, rule in jobid_to_rule.items():
        jobids_by_rule.setdefault(rule, []).append(jobid)
    for jobids in jobids_by_rule.values():
        jobids.sort()

    groups = _group_summary_rows(summary_rows)

    groups_by_rule: dict[str, list[_JobGroup]] = {}
    for group in groups:
        groups_by_rule.setdefault(group.rule, []).append(group)

    for rule, grouped in groups_by_rule.items():
        grouped.sort(key=lambda grp: (sorted(grp.outputs)[:1], grp.shellcmd))
        ids = jobids_by_rule.get(rule, [])
        for index, group in enumerate(grouped):
            if index < len(ids):
                group.jobid = ids[index]

    all_files: set[str] = set()
    for group in groups:
        all_files.update(group.inputs)
        all_files.update(group.outputs)

    file_entities: dict[str, FileEntity] = {}
    for file_path in sorted(all_files):
        entity = FileEntity(id=file_id(file_path), type="prov:Entity")
        info = None
        path = Path(file_path)
        if path.exists():
            info = _path_info(path)
            entity.format = info.get("format")
            entity.extent = info.get("size")
            modified = info.get("modified")
            if modified:
                try:
                    entity.modified = datetime.fromisoformat(modified)
                except ValueError:
                    entity.modified = None
            identifier = info.get("sha256")
            if identifier:
                entity.identifier = f"sha256:{identifier}"
        entity._extra["label"] = file_path
        file_entities[file_path] = entity

    activities: dict[str, ActivityNode] = {}
    group_to_act_id: dict[_JobGroup, str] = {}

    for group in groups:
        activity_id = (
            f"{base}job/{group.jobid}"
            if group.jobid is not None
            else job_fallback_id(group)
        )
        group_to_act_id[group] = activity_id

        used_ids = [file_id(inp) for inp in group.inputs]
        activity = ActivityNode(
            id=activity_id,
            type="prov:Activity",
            startedAtTime=None,
            endedAtTime=None,
            wasAssociatedWith=agent.id,
            used=tuple(used_ids) if used_ids else None,
            comment=None,
        )
        label = group.rule
        if group.jobid is not None:
            label += f" (jobid={group.jobid})"
        activity._extra["label"] = label
        activity._extra["snakemake:rule"] = group.rule
        if group.version and group.version != "-":
            activity._extra["snakemake:version"] = group.version
        if group.shellcmd and group.shellcmd != "-":
            activity._extra["snakemake:shellcmd"] = group.shellcmd
        if group.status and group.status != "-":
            activity._extra["snakemake:status"] = group.status
        if group.plan and group.plan != "-":
            activity._extra["snakemake:plan"] = group.plan

        activities[activity_id] = activity

        for output in group.outputs:
            file_entities[output].wasGeneratedBy = activity_id

    jobid_to_activity: dict[int, str] = {}
    for group in groups:
        if group.jobid is not None:
            jobid_to_activity[group.jobid] = group_to_act_id[group]

    for dependency, job in d3_edges:
        dep_activity = jobid_to_activity.get(dependency)
        job_activity = jobid_to_activity.get(job)
        if not dep_activity or not job_activity:
            continue
        activities[job_activity]._extra.setdefault("wasInformedBy", [])
        activities[job_activity]._extra["wasInformedBy"].append(dep_activity)

    return Prov(
        base_iri=config.base_iri or "",
        name=name,
        provenance=[agent, *activities.values(), *file_entities.values()],
        results=[],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m makeprov.snakemake",
        description=(
            "Generate a makeprov PROV document from a Snakemake workflow via "
            "--d3dag and --detailed-summary."
        ),
    )
    parser.add_argument(
        "-c",
        "--conf",
        action="append",
        default=[],
        help="TOML snippet or @file.toml (same as makeprov).",
    )
    parser.add_argument(
        "--name",
        default="snakemake",
        help="Logical document name.",
    )
    parser.add_argument(
        "--prov-path",
        default=None,
        help="Output path without extension (overrides prov_dir).",
    )
    parser.add_argument(
        "--prov-dir",
        default=None,
        help="Output directory (used when prov-path is omitted).",
    )
    parser.add_argument(
        "--out-fmt",
        choices=["json", "trig"],
        default=None,
    )
    parser.add_argument(
        "--frame",
        choices=["provenance", "results"],
        default=None,
    )
    parser.add_argument(
        "--context",
        action="store_true",
        help="Embed JSON-LD context inline.",
    )
    parser.add_argument(
        "--snakemake",
        default="snakemake",
        help="Snakemake executable.",
    )
    parser.add_argument(
        "--forceall-dag",
        action="store_true",
        help=(
            "Add --forceall to the d3dag call to avoid missing edges from "
            "needrun filtering."
        ),
    )
    parser.add_argument(
        "snakemake_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to snakemake. Prefix with '--' (recommended).",
    )

    namespace = parser.parse_args(argv)

    cfg = ProvenanceConfig(**vars(GLOBAL_CONFIG))
    for toml_ref in namespace.conf:
        apply_config(cfg, toml_ref)

    if namespace.prov_dir is not None:
        cfg.prov_dir = namespace.prov_dir
    if namespace.prov_path is not None:
        cfg.prov_path = namespace.prov_path
    if namespace.out_fmt is not None:
        cfg.out_fmt = namespace.out_fmt  # type: ignore[assignment]
    if namespace.frame is not None:
        cfg.frame = namespace.frame  # type: ignore[assignment]
    if namespace.context:
        cfg.context = True

    smk_args = list(namespace.snakemake_args)
    if smk_args and smk_args[0] == "--":
        smk_args = smk_args[1:]

    dag = get_d3dag_json(
        namespace.snakemake,
        smk_args,
        forceall_dag=namespace.forceall_dag,
    )
    summary = get_detailed_summary(namespace.snakemake, smk_args)

    prov = build_prov_from_snakemake(
        dag,
        summary,
        config=cfg,
        name=namespace.name,
    )

    if cfg.prov_path:
        destination = Path(cfg.prov_path)
    else:
        destination = Path(cfg.prov_dir) / namespace.name

    prov.write(destination, fmt=cfg.out_fmt, frame=cfg.frame, context=cfg.context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
