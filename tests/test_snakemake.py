from __future__ import annotations

import json
from pathlib import Path

import pytest

from makeprov.config import ProvenanceConfig
from makeprov.prov import (
    ActivityNode,
    AgentNode,
    AssociationNode,
    FileEntity,
    PersonNode,
    PlanNode,
)
from makeprov import prov as prov_mod
from makeprov import snakemake as smk


def _patch_cmds(monkeypatch, responses: dict[str, str] | None = None):
    """Stub out shell lookups in both modules.

    ``snakemake.py`` imports ``_safe_cmd`` by value, and the identifier
    heuristic lives in ``prov.py``, so patching one module alone would leave the
    other reading the real git checkout and make these tests depend on wherever
    they happen to run.
    """

    responses = responses or {}
    fake = lambda argv: responses.get(" ".join(argv))  # noqa: E731
    monkeypatch.setattr(smk, "_safe_cmd", fake)
    monkeypatch.setattr(prov_mod, "_safe_cmd", fake)


def test_split_files_whole_cell_existing_path_with_spaces(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spaced = Path("has space.txt")
    spaced.write_text("x", encoding="utf-8")
    assert smk._split_files(str(spaced)) == [str(spaced)]


def test_split_files_multiple_existing_paths(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("a.txt").write_text("a", encoding="utf-8")
    Path("b.txt").write_text("b", encoding="utf-8")
    assert smk._split_files("a.txt b.txt") == ["a.txt", "b.txt"]
    assert smk._split_files("a.txt, b.txt") == ["a.txt", "b.txt"]


def test_split_files_ambiguous_whitespace_raises(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Cannot unambiguously split"):
        smk._split_files("results/file with spaces.txt")


def test_extract_json_blob_with_leading_noise():
    payload = '{"nodes": [], "links": []}'
    noisy = "INFO Running something\n" + payload + "\n"
    assert smk._extract_json_blob(noisy) == json.loads(payload)


def _minimal_workflow():
    dag = {"nodes": [{"id": 1, "value": {"rule": "concat"}}], "links": []}
    summary = [
        {
            "rule": "concat",
            "version": "-",
            "input-file(s)": "a.txt",
            "output_file": "out.txt",
            "shellcmd": "cat {input} > {output}",
            "status": "finished",
            "plan": "shell",
        }
    ]
    return dag, summary


def test_snakemake_rules_become_plans(monkeypatch):
    """The bridge uses the same Plan/Agent split as the decorator API."""

    _patch_cmds(monkeypatch, {"snakemake --version": "7.32.0"})
    dag, summary = _minimal_workflow()

    prov = smk.build_prov_from_snakemake(dag, summary, config=ProvenanceConfig())

    (plan,) = [n for n in prov.provenance if isinstance(n, PlanNode)]
    assert "prov:Plan" in plan.type
    assert plan.label == "concat"

    (agent,) = [n for n in prov.provenance if isinstance(n, AgentNode)]
    assert "prov:SoftwareAgent" in agent.type
    assert "schema:SoftwareSourceCode" not in agent.type

    (assoc,) = [n for n in prov.provenance if isinstance(n, AssociationNode)]
    activity = next(n for n in prov.provenance if isinstance(n, ActivityNode))
    assert assoc.hadPlan == plan.id
    assert assoc.agent == agent.id
    assert activity.qualifiedAssociation == assoc.id


def test_snakemake_mints_no_unregistered_urn_namespace(monkeypatch):
    """RFC 8141 requires a URN's NID to be IANA-registered.

    The bridge used to default to `urn:snakemake:`, which names no real
    namespace and would collide across unrelated workflows sharing rule names.
    """

    _patch_cmds(monkeypatch, {"snakemake --version": "7.32.0"})
    dag, summary = _minimal_workflow()

    prov = smk.build_prov_from_snakemake(dag, summary, config=ProvenanceConfig())

    ids = [node.id for node in prov.provenance]
    assert ids, "expected some nodes"
    assert not any(i.startswith("urn:snakemake") for i in ids)
    # Relative IRIs, resolved against the document base, like the decorator API.
    assert "rule/concat" in ids
    assert "job/1" in ids


def test_snakemake_uses_configured_base_iri(monkeypatch):
    _patch_cmds(monkeypatch, {"snakemake --version": "7.32.0"})
    dag, summary = _minimal_workflow()

    prov = smk.build_prov_from_snakemake(
        dag, summary, config=ProvenanceConfig(base_iri="https://example.org/wf")
    )

    ids = [node.id for node in prov.provenance]
    assert "https://example.org/wf/rule/concat" in ids
    assert "https://example.org/wf/job/1" in ids


def test_snakemake_uses_github_heuristic(monkeypatch, tmp_path: Path):
    """Without a base_iri the bridge derives one from the git remote, exactly
    as the decorator API does."""

    monkeypatch.chdir(tmp_path)
    Path("a.txt").write_text("a", encoding="utf-8")
    _patch_cmds(
        monkeypatch,
        {
            "snakemake --version": "7.32.0",
            "git config --get remote.origin.url": "https://github.com/example/repo.git",
            "git rev-parse HEAD": "abc123",
            "git rev-parse --show-toplevel": str(tmp_path),
        },
    )
    dag, summary = _minimal_workflow()

    prov = smk.build_prov_from_snakemake(dag, summary, config=ProvenanceConfig())

    assert prov.context["@base"] == "https://github.com/example/repo#"
    # Commit-pinned, not branch-pinned.
    assert prov.context["blob"] == "https://github.com/example/repo/blob/abc123/"

    ids = [node.id for node in prov.provenance]
    assert "blob:a.txt" in ids
    assert "rule/concat" in ids


def _two_step_workflow():
    dag = {
        "nodes": [
            {"id": 1, "value": {"rule": "concat"}},
            {"id": 2, "value": {"rule": "count"}},
        ],
        "links": [{"u": 1, "v": 2}],
    }
    summary = [
        {"rule": "concat", "version": "-", "input-file(s)": "a.txt",
         "output_file": "mid.txt", "shellcmd": "cat", "status": "finished", "plan": "shell"},
        {"rule": "count", "version": "-", "input-file(s)": "mid.txt",
         "output_file": "out.txt", "shellcmd": "wc", "status": "finished", "plan": "shell"},
    ]
    return dag, summary


def test_snakemake_plan_graph_is_off_by_default(monkeypatch):
    _patch_cmds(monkeypatch, {"snakemake --version": "7.32.0"})
    dag, summary = _two_step_workflow()

    prov = smk.build_prov_from_snakemake(dag, summary, config=ProvenanceConfig())

    assert all(p.requires is None for p in prov.provenance if isinstance(p, PlanNode))


def test_snakemake_plan_graph_links_rules(monkeypatch):
    """Job-level DAG edges collapse to rule-level dct:requires edges."""

    _patch_cmds(monkeypatch, {"snakemake --version": "7.32.0"})
    dag, summary = _two_step_workflow()

    prov = smk.build_prov_from_snakemake(
        dag, summary, config=ProvenanceConfig(emit_plan_graph=True)
    )

    plans = {p.label: p for p in prov.provenance if isinstance(p, PlanNode)}
    assert plans["concat"].requires is None
    assert plans["count"].requires == (plans["concat"].id,)


def test_snakemake_activity_states_generated(monkeypatch):
    _patch_cmds(monkeypatch, {"snakemake --version": "7.32.0"})
    dag, summary = _minimal_workflow()

    prov = smk.build_prov_from_snakemake(dag, summary, config=ProvenanceConfig())

    activity = next(n for n in prov.provenance if isinstance(n, ActivityNode))
    assert activity.generated == ("file/out.txt",)


def test_snakemake_person_is_opt_in(monkeypatch):
    responses = {
        "snakemake --version": "7.32.0",
        "git config --get user.name": "Ada Lovelace",
        "git config --get user.email": "ada@example.org",
    }
    _patch_cmds(monkeypatch, responses)
    dag, summary = _minimal_workflow()

    default = smk.build_prov_from_snakemake(dag, summary, config=ProvenanceConfig())
    assert [n for n in default.provenance if isinstance(n, PersonNode)] == []
    assert "ada@example.org" not in str(default.to_jsonld())

    opted_in = smk.build_prov_from_snakemake(
        dag, summary, config=ProvenanceConfig(record_user=True)
    )
    (person,) = [n for n in opted_in.provenance if isinstance(n, PersonNode)]
    assert person.id == "mailto:ada@example.org"


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

    _patch_cmds(monkeypatch, {"snakemake --version": "7.32.0"})

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

    second_job = activities["job/2"]
    assert second_job._extra.get("wasInformedBy") == ["job/1"]
    assert second_job._extra["snakemake:rule"] == "count_words"

    assert files[str(results_dir / "word_count.txt")].wasGeneratedBy == second_job.id
    assert files[str(results_dir / "word_count.txt")].id.startswith("file/")


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
    _patch_cmds(monkeypatch, {"snakemake --version": "7.32.0"})

    prov_path = tmp_path / "out" / "workflow"
    exit_code = smk.main(["--prov-path", str(prov_path), "--snakemake", "snakemake"])
    assert exit_code == 0

    output = prov_path.with_suffix(".json")
    data = json.loads(output.read_text(encoding="utf-8"))
    assert "provenance" in data
    assert any(entry.get("label") == "count (jobid=2)" for entry in data["provenance"])
