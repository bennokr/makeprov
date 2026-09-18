"""ReproZip graph fixtures follow reprounzip/unpackers/graph.py's JSON schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from makeprov.config import ProvenanceConfig
from makeprov.prov import ActivityNode, AgentNode, AssociationNode, FileEntity, PlanNode
from makeprov.reprozip import build_prov_from_reprozip, convert_reprozip_graph, main


@pytest.fixture
def graph():
    return {
        "packages": [{"name": "python", "version": "3.11", "files": ["/usr/bin/python"]}],
        "other_files": ["/data/in.txt", "/data/out.txt", "/data/with space.txt"],
        "inputs_outputs": [
            {"name": "important", "path": "/data/in.txt", "read_by_runs": [0], "written_by_runs": []},
            # This manifest path has been filtered out of the graph.
            {"name": "filtered", "path": "/data/filtered.txt", "read_by_runs": [0], "written_by_runs": []},
        ],
        "runs": [{
            "name": "first run",
            "processes": [
                {"name": "71", "long_name": "sh (71)", "description": "/bin/sh\n71",
                 "argv": ["sh", "script.sh"], "start_time": 5, "is_thread": False,
                 "parent": None, "reads": ["/data/in.txt"], "writes": ["/data/out.txt"]},
                # Same PID after exec; the parent is process index 0.
                {"name": "71", "long_name": "python (71)", "description": "/usr/bin/python\n71",
                 "argv": ["python", "script.py"], "start_time": 8, "is_thread": False,
                 "parent": [0, "exec"], "reads": ["/usr/bin/python", "/data/with space.txt"],
                 "writes": []},
            ],
        }],
    }


def _convert(graph):
    return build_prov_from_reprozip(
        graph, config=ProvenanceConfig(base_iri="https://example.org/prov/"), name="experiment"
    )


def test_observed_reads_writes_and_no_unobserved_manifest_entries(graph):
    prov = _convert(graph)
    activities = [n for n in prov.provenance if isinstance(n, ActivityNode)]
    assert len(activities) == 2
    first, second = sorted(activities, key=lambda n: n.id)
    assert first.used == ("file:///data/in.txt",)
    assert first.generated == ("file:///data/out.txt",)
    assert second.used == ("file:///usr/bin/python", "file:///data/with%20space.txt")
    assert second.generated is None
    assert all("filtered.txt" not in n.id for n in prov.provenance)
    output = next(n for n in prov.provenance if isinstance(n, FileEntity) and n.id == "file:///data/out.txt")
    assert output.wasGeneratedBy == first.id
    assert "semantic dependency" in first.comment


def test_parent_is_run_local_index_not_pid(graph):
    prov = _convert(graph)
    activities = sorted((n for n in prov.provenance if isinstance(n, ActivityNode)), key=lambda n: n.id)
    assert activities[1]._extra["wasInformedBy"] == [activities[0].id]
    properties = activities[1]._extra["schema:additionalProperty"]
    assert any(p["schema:name"] == "ReproZip parent event" and p["schema:value"] == "exec" for p in properties)


def test_raw_numeric_timestamp_is_not_assumed_to_be_unix_seconds(graph):
    activity = next(n for n in _convert(graph).provenance if isinstance(n, ActivityNode))
    assert activity.startedAtTime is None
    assert any(p["schema:value"] == 5 for p in activity._extra["schema:additionalProperty"])


def test_iso8601_timestamp_is_supported(graph):
    graph["runs"][0]["processes"][0]["start_time"] = "2026-09-17T11:00:00+02:00"
    activity = next(n for n in _convert(graph).provenance if isinstance(n, ActivityNode))
    assert activity.startedAtTime.isoformat() == "2026-09-17T11:00:00+02:00"


def test_process_agent_plan_and_association_are_distinct(graph):
    prov = _convert(graph)
    acts = [n for n in prov.provenance if isinstance(n, ActivityNode)]
    agents = [n for n in prov.provenance if isinstance(n, AgentNode)]
    plans = [n for n in prov.provenance if isinstance(n, PlanNode)]
    associations = [n for n in prov.provenance if isinstance(n, AssociationNode)]
    assert len(acts) == len(agents) == len(plans) == len(associations) == 2
    assert all("prov:SoftwareAgent" in agent.type for agent in agents)
    assert all("prov:Plan" in plan.type for plan in plans)
    assert associations[0].hadPlan == plans[0].id
    assert acts[0].qualifiedAssociation == associations[0].id


def test_package_metadata_and_percent_encoded_file_iris(graph):
    prov = _convert(graph)
    package = next(n for n in prov.provenance if isinstance(n, FileEntity) and n._extra.get("hasVersion"))
    assert package._extra["hasVersion"] == "3.11"
    binary = next(n for n in prov.provenance if isinstance(n, FileEntity) and n.id == "file:///usr/bin/python")
    assert binary._extra["isPartOf"] == package.id
    assert "file:///data/with%20space.txt" in [n.id for n in prov.provenance]


def test_multiple_writers_get_distinct_generated_entities(graph):
    graph["runs"][0]["processes"][1]["writes"] = ["/data/out.txt"]
    prov = _convert(graph)
    first, second = sorted((n for n in prov.provenance if isinstance(n, ActivityNode)), key=lambda n: n.id)
    assert first.generated != second.generated
    assert all(n.id != "file:///data/out.txt" for n in prov.provenance if isinstance(n, FileEntity) and n.wasGeneratedBy)
    for activity in (first, second):
        (output_id,) = activity.generated
        entity = next(n for n in prov.provenance if isinstance(n, FileEntity) and n.id == output_id)
        assert entity.wasGeneratedBy == activity.id
        assert entity._extra["source"] == "file:///data/out.txt"


def test_read_and_write_same_file_uses_separate_output_identity(graph):
    proc = graph["runs"][0]["processes"][0]
    proc["writes"] = ["/data/in.txt"]
    prov = _convert(graph)
    activity = next(n for n in prov.provenance if isinstance(n, ActivityNode))
    assert activity.used == ("file:///data/in.txt",)
    assert activity.generated != activity.used


def test_jsonld_expands_into_valid_prov_rdf(graph):
    rdflib = pytest.importorskip("rdflib")
    prov = _convert(graph)
    doc = prov.to_jsonld(with_context=True)
    rdf = rdflib.Graph().parse(data=json.dumps(doc), format="json-ld")
    PROV = rdflib.Namespace("http://www.w3.org/ns/prov#")
    activities = [n for n in prov.provenance if isinstance(n, ActivityNode)]
    first, second = sorted(activities, key=lambda n: n.id)
    assert (rdflib.URIRef(first.id), PROV.used, rdflib.URIRef("file:///data/in.txt")) in rdf
    assert (rdflib.URIRef(second.id), PROV.wasInformedBy, rdflib.URIRef(first.id)) in rdf
    assert (rdflib.URIRef("file:///data/out.txt"), PROV.wasGeneratedBy, rdflib.URIRef(first.id)) in rdf


def test_rejects_bad_parent_and_bad_files(graph):
    graph["runs"][0]["processes"][1]["parent"] = [71, "exec"]  # PID, not index
    with pytest.raises(ValueError, match="earlier process index"):
        _convert(graph)
    graph["runs"][0]["processes"][1]["parent"] = [0, "exec"]
    graph["runs"][0]["processes"][1]["reads"] = "/data/in.txt"
    with pytest.raises(ValueError, match="array of nonempty file paths"):
        _convert(graph)


def test_file_conversion_and_cli_without_reprozip_install(graph, tmp_path):
    source = tmp_path / "reprozip-graph.json"
    source.write_text(json.dumps(graph), encoding="utf-8")
    assert isinstance(convert_reprozip_graph(source), object)
    stem = tmp_path / "output"
    assert main([str(source), "--output", str(stem), "--base-iri", "https://example.org/prov/"]) == 0
    output = stem.with_suffix(".json")
    assert output.is_file()
    doc = json.loads(output.read_text(encoding="utf-8"))
    assert "wasInformedBy" in doc["@context"]
    assert len(doc["provenance"]) > 0


def test_same_graph_has_stable_ids_and_different_graph_has_different_run_ids(graph):
    a = _convert(graph)
    b = _convert(graph)
    activity_ids = lambda prov: {n.id for n in prov.provenance if isinstance(n, ActivityNode)}
    assert activity_ids(a) == activity_ids(b)
    graph["runs"][0]["processes"][0]["argv"][1] = "different.sh"
    assert activity_ids(a).isdisjoint(activity_ids(_convert(graph)))
