from __future__ import annotations

import json
from pathlib import Path

import pytest

from makeprov.config import ProvenanceConfig
from makeprov.prov import ActivityNode, AgentNode, FileEntity
from makeprov import snakemake as smk


def test_extract_json_blob_with_leading_noise():
    payload = '{"nodes": [], "links": []}'
    noisy = "INFO Running something\n" + payload + "\n"
    assert smk._extract_json_blob(noisy) == json.loads(payload)


def test_build_prov_from_snakemake_generates_edges(monkeypatch, tmp_path: Path):
    dag = {
        "nodes": [
            {"id": 1, "value": {"rule": "concat"}},
            {"id": 2, "value": {"rule": "count_words"}},
        ],
        "links": [{"u": 1, "v": 2}],
    }
    data_dir = tmp_path / "data"
    results_dir = tmp_path / "results"
    data_dir.mkdir()
    results_dir.mkdir()

    (data_dir / "a.txt").write_text("a\n", encoding="utf-8")
    (data_dir / "b.txt").write_text("b\n", encoding="utf-8")
    (results_dir / "concatenated.txt").write_text("a\nb\n", encoding="utf-8")
    (results_dir / "word_count.txt").write_text("2 results/concatenated.txt\n", encoding="utf-8")

    summary = [
        {
            "rule": "concat",
            "version": "-",
            "input-file(s)": f"{data_dir/'a.txt'} {data_dir/'b.txt'}",
            "output_file": str(results_dir / "concatenated.txt"),
            "shellcmd": "cat {input} > {output}",
            "status": "finished",
            "plan": "shell",
        },
        {
            "rule": "count_words",
            "version": "-",
            "input-file(s)": str(results_dir / "concatenated.txt"),
            "output_file": str(results_dir / "word_count.txt"),
            "shellcmd": "wc -w {input} > {output}",
            "status": "finished",
            "plan": "shell",
        },
    ]

    monkeypatch.setattr(smk, "_safe_cmd", lambda _: "7.32.0")

    prov = smk.build_prov_from_snakemake(
        dag,
        summary,
        config=ProvenanceConfig(),
        name="snakemake",
    )

    agent = next(node for node in prov.provenance if isinstance(node, AgentNode))
    assert agent.hasVersion == "7.32.0"

    activities = {node.id: node for node in prov.provenance if isinstance(node, ActivityNode)}
    files = {node._extra["label"]: node for node in prov.provenance if isinstance(node, FileEntity)}

    second_job = activities["urn:snakemake:job/2"]
    assert second_job._extra.get("wasInformedBy") == ["urn:snakemake:job/1"]
    assert second_job._extra["snakemake:rule"] == "count_words"

    assert files[str(results_dir / "word_count.txt")].wasGeneratedBy == second_job.id
    assert "urn:snakemake:file" in files[str(results_dir / "word_count.txt")].id


def test_main_writes_provenance_document(monkeypatch, tmp_path: Path):
    dag = {
        "nodes": [
            {"id": 1, "value": {"rule": "concat"}},
            {"id": 2, "value": {"rule": "count"}},
        ],
        "links": [{"u": 1, "v": 2}],
    }
    summary = [
        {
            "rule": "concat",
            "version": "-",
            "input-file(s)": "a.txt",
            "output_file": str(tmp_path / "concatenated.txt"),
            "shellcmd": "echo concat",
            "status": "finished",
            "plan": "shell",
        },
        {
            "rule": "count",
            "version": "-",
            "input-file(s)": str(tmp_path / "concatenated.txt"),
            "output_file": str(tmp_path / "count.txt"),
            "shellcmd": "echo count",
            "status": "finished",
            "plan": "shell",
        },
    ]

    monkeypatch.setattr(smk, "get_d3dag_json", lambda *_, **__: dag)
    monkeypatch.setattr(smk, "get_detailed_summary", lambda *_, **__: summary)
    monkeypatch.setattr(smk, "_safe_cmd", lambda _: "7.32.0")

    prov_path = tmp_path / "out" / "workflow"
    exit_code = smk.main(["--prov-path", str(prov_path), "--snakemake", "snakemake"])
    assert exit_code == 0

    output = prov_path.with_suffix(".json")
    data = json.loads(output.read_text(encoding="utf-8"))
    assert "provenance" in data
    assert any(entry.get("label") == "count (jobid=2)" for entry in data["provenance"])
